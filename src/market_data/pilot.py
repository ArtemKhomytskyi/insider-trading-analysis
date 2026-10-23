"""Explicit, isolated Phase 5.2 pilot. Never writes to the source SEC database."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from src.market_data.providers.tiingo import TiingoProvider, UnknownTicker


SCHEMA = """
CREATE TABLE IF NOT EXISTS pilot_securities (
 security_id TEXT PRIMARY KEY, issuer_cik TEXT NOT NULL, ticker TEXT NOT NULL,
 provider_ticker TEXT NOT NULL, verified_provider_name TEXT NOT NULL,
 provider_security_id TEXT, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
 review_status TEXT NOT NULL, selection_reason TEXT NOT NULL,
 mapping_method TEXT NOT NULL, mapping_confidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_prices (
 security_id TEXT NOT NULL, provider TEXT NOT NULL, provider_security_id TEXT,
 ticker TEXT NOT NULL, trading_date TEXT NOT NULL,
 open REAL, high REAL, low REAL, close REAL, adj_open REAL, adj_high REAL,
 adj_low REAL, adj_close REAL, volume REAL, adj_volume REAL,
 split_factor REAL, dividend_cash REAL, adjustment_basis TEXT NOT NULL,
 downloaded_at TEXT NOT NULL,
 PRIMARY KEY(security_id, provider, trading_date)
);
CREATE TABLE IF NOT EXISTS pilot_requests (
 security_id TEXT NOT NULL, provider TEXT NOT NULL, requested_start TEXT NOT NULL,
 requested_end TEXT NOT NULL, status TEXT NOT NULL, row_count INTEGER NOT NULL,
 attempts INTEGER NOT NULL, error TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(security_id, provider, requested_start, requested_end)
);
"""

FIELDS = ["security_id", "issuer_cik", "ticker", "provider_ticker", "verified_provider_name",
          "provider_security_id", "valid_from",
          "valid_to", "review_status", "selection_reason", "mapping_method", "mapping_confidence"]


def select_manifest(source_db: Path, output: Path, count: int = 200) -> int:
    if not 100 <= count <= 300:
        raise ValueError("Pilot count must be between 100 and 300")
    conn = sqlite3.connect(f"file:{source_db.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Indexed mapping table is small; never scan the multi-million-row transaction table.
        candidates = conn.execute("""
          SELECT mapping_id security_id, issuer_cik, market_data_symbol ticker,
                 valid_from, valid_to, mapping_method, mapping_confidence,
                 is_ticker_change, is_reused_ticker, is_delisted, security_class
          FROM security_identifier_mapping
          WHERE is_ambiguous = 0 AND market_data_symbol IS NOT NULL
            AND valid_from IS NOT NULL AND valid_to IS NOT NULL
          ORDER BY mapping_id
        """).fetchall()
    finally:
        conn.close()
    # Avoid a single vendor ticker request silently spanning unrelated identities.
    ticker_ids: dict[str, set[str]] = {}
    for row in candidates:
        ticker_ids.setdefault(row["ticker"], set()).add(row["issuer_cik"])
    eligible = [r for r in candidates if len(ticker_ids[r["ticker"]]) == 1 and
                not r["is_reused_ticker"] and
                re.fullmatch(r"[A-Z]{1,5}(?:[.-][A-Z])?", r["ticker"]) and
                not (len(r["ticker"]) == 5 and r["ticker"].endswith("X")) and
                (date.fromisoformat(r["valid_to"]) - date.fromisoformat(r["valid_from"])).days >= 180]
    if len(eligible) < count:
        raise ValueError(f"Only {len(eligible)} non-reused candidates available")
    # Deterministic strata: early/late periods, ticker changes, possible inactive names.
    buckets: dict[str, list[sqlite3.Row]] = {}
    for r in eligible:
        era = "early" if r["valid_from"][:4] < "2015" else "late"
        flag = "change" if r["is_ticker_change"] else "inactive" if r["is_delisted"] else "other"
        buckets.setdefault(f"{era}_{flag}", []).append(r)
    chosen: list[sqlite3.Row] = []
    used: set[str] = set()
    keys = sorted(buckets)
    while len(chosen) < count and keys:
        for key in keys[:]:
            if not buckets[key]:
                keys.remove(key)
                continue
            r = buckets[key].pop(0)
            if r["ticker"] not in used:
                chosen.append(r)
                used.add(r["ticker"])
                if len(chosen) == count:
                    break
    if len(chosen) < count:
        raise ValueError("Insufficient distinct safe tickers for requested pilot")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for r in chosen:
            writer.writerow(dict(security_id=r["security_id"], issuer_cik=r["issuer_cik"],
                ticker=r["ticker"], provider_ticker=r["ticker"].replace(".", "-"),
                verified_provider_name="", provider_security_id="", valid_from=r["valid_from"],
                valid_to=r["valid_to"], review_status="PENDING", selection_reason=(
                    "ticker_change" if r["is_ticker_change"] else
                    "possible_inactive" if r["is_delisted"] else "period_diversity"),
                mapping_method=r["mapping_method"], mapping_confidence=r["mapping_confidence"]))
    return len(chosen)


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not 1 <= len(rows) <= 200 or any(not all(r.get(k) for k in
            ("security_id", "issuer_cik", "ticker", "provider_ticker",
             "verified_provider_name", "valid_from", "valid_to")) for r in rows):
        raise ValueError("Manifest must contain 1-200 complete, vendor-reviewed identities")
    if len({r["security_id"] for r in rows}) != len(rows) or len({r["provider_ticker"] for r in rows}) != len(rows):
        raise ValueError("Duplicate security IDs or vendor tickers require separate resolution")
    if any(r.get("review_status") != "APPROVED" for r in rows):
        raise ValueError("Every identity requires explicit APPROVED review_status")
    return rows


def normalized_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def run_pilot(manifest: Path, pilot_db: Path, provider: TiingoProvider,
              start: str, end: str, retry_count: int = 2, delay: float = 0.0) -> dict[str, int]:
    if retry_count < 0 or retry_count > 2:
        raise ValueError("Pilot permits at most two retries per API operation")
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Start date must precede end date")
    rows = read_manifest(manifest)
    production_db = Path(__file__).resolve().parents[2] / "data/processed/insider_research.db"
    if pilot_db.resolve() in (manifest.resolve(), production_db.resolve()):
        raise ValueError("Pilot database must be separate from manifest and production database")
    pilot_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(pilot_db)
    conn.executescript(SCHEMA)
    stats = {"complete": 0, "partial": 0, "failed": 0, "no_data": 0,
             "unknown_symbol": 0, "identity_review": 0, "skipped": 0, "api_requests": 0}
    try:
        for r in rows:
            sid, ticker = r["security_id"], r["provider_ticker"]
            request_start = max(start, r["valid_from"])
            request_end = min(end, r["valid_to"])
            if request_start > request_end:
                stats["skipped"] += 1
                continue
            prior = conn.execute("SELECT status FROM pilot_requests WHERE security_id=? AND provider=? AND requested_start=? AND requested_end=?",
                (sid, provider.name, request_start, request_end)).fetchone()
            if prior and prior[0] == "COMPLETE":
                stats["skipped"] += 1
                continue
            error = None
            prices = []
            attempts = 0
            metadata = None
            terminal_status = None
            for attempt in range(retry_count + 1):
                attempts += 1
                stats["api_requests"] += 1
                try:
                    metadata = provider.get_metadata(ticker)
                    error = None
                    break
                except UnknownTicker as exc:
                    error, terminal_status = str(exc), "UNKNOWN_SYMBOL"
                    break
                except Exception as exc:
                    error = str(exc)
                    if attempt < retry_count:
                        time.sleep(min(2 ** attempt, 30))
            if metadata is not None:
                if (metadata["ticker"].upper() != ticker.upper() or
                    normalized_name(metadata["name"]) != normalized_name(r["verified_provider_name"])):
                    terminal_status = "IDENTITY_REVIEW"
                    error = "Tiingo metadata ticker/name does not match reviewed manifest identity"
                elif (metadata.get("startDate") and metadata["startDate"] > request_end) or (
                      metadata.get("endDate") and metadata["endDate"] < request_start):
                    terminal_status = "IDENTITY_REVIEW"
                    error = "Tiingo metadata availability does not overlap mapping interval"
            if metadata is not None and terminal_status is None:
                for attempt in range(retry_count + 1):
                    attempts += 1
                    stats["api_requests"] += 1
                    try:
                        prices = provider.get_daily_prices(ticker, request_start, request_end)
                        error = None
                        break
                    except UnknownTicker as exc:
                        error, terminal_status = str(exc), "UNKNOWN_SYMBOL"
                        break
                    except Exception as exc:
                        error = str(exc)
                        if attempt < retry_count:
                            time.sleep(min(2 ** attempt, 30))
            valid = [p for p in prices if request_start <= p.trading_date <= request_end and p.market_data_symbol == ticker]
            status = terminal_status or ("FAILED" if error else "NO_DATA" if not prices else
                "PARTIAL" if len(valid) != len(prices) else "COMPLETE")
            if status == "COMPLETE" and metadata is not None:
                expected_start = max(request_start, metadata.get("startDate") or request_start)
                expected_end = min(request_end, metadata.get("endDate") or request_end)
                observed = sorted(p.trading_date for p in valid)
                if (date.fromisoformat(observed[0]) - date.fromisoformat(expected_start)).days > 7 or (
                    date.fromisoformat(expected_end) - date.fromisoformat(observed[-1])).days > 7:
                    status = "PARTIAL"
                    error = "Response does not cover metadata-overlapping date window"
            now = datetime.now(timezone.utc).isoformat()
            with conn:
                for p in valid:
                    conn.execute("""INSERT OR REPLACE INTO pilot_prices VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (sid, provider.name,
                      r.get("provider_security_id") or None, ticker, p.trading_date,
                      p.open, p.high, p.low, p.close, p.adjusted_open, p.adjusted_high,
                      p.adjusted_low, p.adjusted_close, p.volume, p.adjusted_volume,
                      p.split_factor, p.dividend_cash, p.adjustment_basis, now))
                conn.execute("""INSERT OR REPLACE INTO pilot_requests VALUES (?,?,?,?,?,?,?,?,?)""",
                    (sid, provider.name, request_start, request_end, status, len(valid), attempts, error, now))
                conn.execute("""INSERT OR REPLACE INTO pilot_securities VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(r.get(k) or None for k in FIELDS))
            stats[status.lower()] += 1
            if delay:
                time.sleep(delay)
    finally:
        conn.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled isolated market-data pilot")
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select")
    select.add_argument("--source-db", type=Path, required=True)
    select.add_argument("--manifest", type=Path, required=True)
    select.add_argument("--count", type=int, default=200)
    run = sub.add_parser("run")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--pilot-db", type=Path, required=True)
    run.add_argument("--start-date", required=True)
    run.add_argument("--end-date", required=True)
    run.add_argument("--rate-limit-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if args.command == "select":
        print(select_manifest(args.source_db, args.manifest, args.count))
    else:
        if not os.environ.get("TIINGO_API_KEY"):
            parser.error("TIINGO_API_KEY is required")
        production_db = Path(__file__).resolve().parents[2] / "data/processed/insider_research.db"
        if args.pilot_db.resolve() == production_db.resolve() or args.pilot_db.resolve() == args.manifest.resolve():
            parser.error("Refusing to write to production SEC database")
        print(json.dumps(run_pilot(args.manifest, args.pilot_db, TiingoProvider(),
              args.start_date, args.end_date, delay=args.rate_limit_seconds)))


if __name__ == "__main__":
    main()
