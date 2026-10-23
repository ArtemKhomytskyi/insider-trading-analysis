"""Ten-security Yahoo experiment, isolated from the production downloader/database."""

from __future__ import annotations

import argparse
import csv
from contextlib import closing
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time

from src.market_data.providers.yahoo_pilot import YahooPilotProvider


FIELDS = ("security_id", "issuer_cik", "ticker", "provider_ticker",
          "company_name_hint", "valid_from", "valid_to", "mapping_confidence",
          "known_ticker_reuse_issuer_count", "ticker_change_flag", "inactive_observation_flag",
          "review_status", "identity_evidence")

SCHEMA = """
CREATE TABLE IF NOT EXISTS yahoo_pilot_metadata (
 provider TEXT PRIMARY KEY CHECK(provider='yahoo')
);
CREATE TABLE IF NOT EXISTS pilot_securities (
 security_id TEXT PRIMARY KEY, issuer_cik TEXT NOT NULL, ticker TEXT NOT NULL,
 provider_ticker TEXT NOT NULL, company_name_hint TEXT, valid_from TEXT NOT NULL,
 valid_to TEXT NOT NULL, mapping_confidence TEXT, known_ticker_reuse_issuer_count INTEGER,
 ticker_change_flag INTEGER, inactive_observation_flag INTEGER,
 review_status TEXT NOT NULL, identity_evidence TEXT NOT NULL
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


def readonly_connection(source_db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{source_db.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def select_manifest(source_db: Path, output: Path) -> list[dict[str, str]]:
    with closing(readonly_connection(source_db)) as conn:
        candidates = conn.execute("""
            SELECT m.mapping_id, m.issuer_cik, m.market_data_symbol,
                   m.valid_from, m.valid_to, m.mapping_confidence,
                   m.is_ticker_change, m.is_delisted,
                   c.canonical_company_name, c.mvp_transaction_count,
                   (SELECT COUNT(DISTINCT x.issuer_cik)
                      FROM security_identifier_mapping x
                     WHERE x.ticker_clean=m.ticker_clean) AS ticker_issuer_count
              FROM security_identifier_mapping m
              LEFT JOIN company_dimension c ON c.issuer_cik=m.issuer_cik
             WHERE m.is_ambiguous=0 AND m.is_reused_ticker=0
               AND m.market_data_symbol IS NOT NULL
               AND m.valid_from IS NOT NULL AND m.valid_to IS NOT NULL
             ORDER BY m.mapping_id
        """).fetchall()
    eligible = [r for r in candidates if r["ticker_issuer_count"] == 1 and
                re.fullmatch(r"[A-Z]{1,5}(?:[.-][A-Z])?", r["market_data_symbol"]) and
                not (len(r["market_data_symbol"]) == 5 and r["market_data_symbol"].endswith("X")) and
                (date.fromisoformat(r["valid_to"]) - date.fromisoformat(r["valid_from"])).days >= 180]
    groups = {
        "early_change": [r for r in eligible if r["valid_from"][:4] < "2015" and r["is_ticker_change"]],
        "early_other": [r for r in eligible if r["valid_from"][:4] < "2015" and not r["is_ticker_change"]],
        "late_change": [r for r in eligible if r["valid_from"][:4] >= "2015" and r["is_ticker_change"]],
        "late_other": [r for r in eligible if r["valid_from"][:4] >= "2015" and not r["is_ticker_change"]],
    }
    for group in groups.values():
        group.sort(key=lambda r: (-(r["mvp_transaction_count"] or 0), r["mapping_id"]))
    targets = {"early_change": 3, "early_other": 2, "late_change": 3, "late_other": 2}
    chosen = []
    used_tickers = set()
    for group, count in targets.items():
        group_selected = 0
        for r in groups[group]:
            provider_ticker = r["market_data_symbol"].replace(".", "-")
            if provider_ticker in used_tickers:
                continue
            chosen.append(r)
            used_tickers.add(provider_ticker)
            group_selected += 1
            if group_selected >= count:
                break
    if len(chosen) != 10:
        raise ValueError(f"Could select only {len(chosen)} distinct accepted mappings")
    rows = [dict(security_id=r["mapping_id"], issuer_cik=r["issuer_cik"],
                 ticker=r["market_data_symbol"], provider_ticker=r["market_data_symbol"].replace(".", "-"),
                 company_name_hint=r["canonical_company_name"] or "",
                 valid_from=r["valid_from"], valid_to=r["valid_to"],
                 mapping_confidence=r["mapping_confidence"],
                 known_ticker_reuse_issuer_count=str(r["ticker_issuer_count"]),
                 ticker_change_flag=str(r["is_ticker_change"]),
                 inactive_observation_flag=str(r["is_delisted"]),
                 review_status="PENDING", identity_evidence="") for r in chosen]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def read_manifest(manifest: Path, source_db: Path) -> list[dict[str, str]]:
    with manifest.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not 1 <= len(rows) <= 10 or any(not all(r.get(field) for field in
            ("security_id", "issuer_cik", "ticker", "provider_ticker", "valid_from", "valid_to",
             "identity_evidence")) or r.get("review_status") != "APPROVED" for r in rows):
        raise ValueError("Yahoo pilot requires at most 10 independently reviewed identities")
    if len({r["security_id"] for r in rows}) != len(rows) or len({r["provider_ticker"] for r in rows}) != len(rows):
        raise ValueError("Duplicate security IDs or Yahoo tickers require review")
    with closing(readonly_connection(source_db)) as conn:
        for row in rows:
            mapping = conn.execute("SELECT * FROM security_identifier_mapping WHERE mapping_id=?",
                                   (row["security_id"],)).fetchone()
            if mapping is None or mapping["is_ambiguous"] or mapping["is_reused_ticker"] or any(
                str(mapping[key]) != row[field] for key, field in
                (("issuer_cik", "issuer_cik"), ("market_data_symbol", "ticker"),
                 ("valid_from", "valid_from"), ("valid_to", "valid_to"))):
                raise ValueError(f"Source mapping mismatch or ambiguity for {row['security_id']}")
            count = conn.execute("SELECT COUNT(DISTINCT issuer_cik) FROM security_identifier_mapping WHERE ticker_clean=?",
                                 (row["ticker"],)).fetchone()[0]
            if count != 1 or row["provider_ticker"] != row["ticker"].replace(".", "-"):
                raise ValueError(f"Ticker reuse or unverified symbol translation for {row['security_id']}")
    return rows


def run_pilot(manifest: Path, source_db: Path, pilot_db: Path, provider: YahooPilotProvider,
              start: str, end: str, retry_count: int = 1, delay: float = 2.0) -> dict[str, int]:
    if not 0 <= retry_count <= 1 or delay < 0:
        raise ValueError("Yahoo pilot permits at most one retry and nonnegative pacing")
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Start date must precede end date")
    production_db = Path(__file__).resolve().parents[2] / "data/processed/insider_research.db"
    if pilot_db.resolve() in (source_db.resolve(), manifest.resolve(), production_db.resolve()):
        raise ValueError("Pilot database cannot be the production database or manifest")
    rows = read_manifest(manifest, source_db)
    pilot_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(pilot_db)
    existing_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if existing_tables and "yahoo_pilot_metadata" not in existing_tables:
        conn.close()
        raise ValueError("Existing database is not a Yahoo pilot database")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO yahoo_pilot_metadata VALUES ('yahoo')")
    conn.commit()
    stats = {"complete": 0, "partial": 0, "failed": 0, "no_data": 0,
             "skipped": 0, "history_calls": 0}
    try:
        for r in rows:
            request_start, request_end = max(start, r["valid_from"]), min(end, r["valid_to"])
            if request_start > request_end:
                stats["skipped"] += 1
                continue
            prior = conn.execute("""SELECT status FROM pilot_requests WHERE
                security_id=? AND provider='yahoo' AND requested_start=? AND requested_end=?""",
                (r["security_id"], request_start, request_end)).fetchone()
            if prior and prior[0] == "COMPLETE":
                stats["skipped"] += 1
                continue
            prices, error, attempts = [], None, 0
            for attempt in range(retry_count + 1):
                attempts += 1
                stats["history_calls"] += 1
                try:
                    prices = provider.get_daily_prices(r["provider_ticker"], request_start, request_end)
                    error = None
                    break
                except Exception as exc:
                    error = str(exc)
                    if attempt < retry_count:
                        time.sleep(delay)
            valid = [p for p in prices if request_start <= p.trading_date <= request_end and
                     p.market_data_symbol == r["provider_ticker"]]
            status = "FAILED" if error else "NO_DATA" if not prices else (
                "PARTIAL" if len(valid) != len(prices) else "COMPLETE")
            if status == "COMPLETE":
                dates = sorted(date.fromisoformat(p.trading_date) for p in valid)
                if (dates[0]-date.fromisoformat(request_start)).days > 7 or (
                    date.fromisoformat(request_end)-dates[-1]).days > 7:
                    status, error = "PARTIAL", "Historical boundary coverage exceeds seven calendar days"
            now = datetime.now(timezone.utc).isoformat()
            with conn:
                for p in valid:
                    conn.execute("""INSERT OR REPLACE INTO pilot_prices VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        r["security_id"], "yahoo", None, r["provider_ticker"], p.trading_date,
                        p.open, p.high, p.low, p.close, None, None, None, p.adjusted_close,
                        p.volume, None, p.split_factor, p.dividend_cash, p.adjustment_basis, now))
                conn.execute("INSERT OR REPLACE INTO pilot_requests VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["security_id"], "yahoo", request_start, request_end, status,
                     len(valid), attempts, error, now))
                conn.execute("INSERT OR REPLACE INTO pilot_securities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(r.get(key) for key in FIELDS))
            stats[status.lower()] += 1
            if delay:
                time.sleep(delay)
    finally:
        conn.close()
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description="Isolated ten-security Yahoo Finance experiment")
    sub = p.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select")
    select.add_argument("--source-db", type=Path, required=True)
    select.add_argument("--manifest", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--source-db", type=Path, required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--pilot-db", type=Path, required=True)
    run.add_argument("--start-date", required=True)
    run.add_argument("--end-date", required=True)
    run.add_argument("--rate-limit-seconds", type=float, default=2.0)
    args = p.parse_args()
    if args.command == "select":
        print(f"Selected {len(select_manifest(args.source_db, args.manifest))} pending identities")
    else:
        print(json.dumps(run_pilot(args.manifest, args.source_db, args.pilot_db,
            YahooPilotProvider(), args.start_date, args.end_date,
            delay=args.rate_limit_seconds)))


if __name__ == "__main__":
    main()
