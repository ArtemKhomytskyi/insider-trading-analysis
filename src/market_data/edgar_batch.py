"""Offline-prepared, bounded and resume-safe EDGAR accession batches."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.market_data.edgar_full_manifest import FIELDS
from src.market_data.edgar_timestamp_pilot import (
    APPROVED, PREFLIGHT_MANIFEST_SHA256, archive_url, fetch_bounded,
    raw_acceptance_values, readonly_db, sanitize_error, verify_header,
)

MAX_BATCH_FILINGS = 100
MAX_BATCH_ATTEMPTS = 300
MAX_FILING_ATTEMPTS = 3
PACE_SECONDS = 1.25


def prepare_first_batch(full_manifest: Path, pilot_manifest: Path, output: Path) -> dict:
    if hashlib.sha256(pilot_manifest.read_bytes()).hexdigest() != PREFLIGHT_MANIFEST_SHA256:
        raise ValueError("Pilot accession manifest hash changed")
    with full_manifest.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 6067 or len({r["accession_number"] for r in rows}) != 6067:
        raise ValueError("Expected 6,067 distinct full-universe accessions")
    with pilot_manifest.open(encoding="utf-8", newline="") as handle:
        excluded = {r["accession_number"] for r in csv.DictReader(handle)}
    quotas = {"CRM": 50, "NFLX": 30, "TSLA": 20}
    selected = []
    for ticker, quota in quotas.items():
        pool = sorted((r for r in rows if r["ticker"] == ticker and r["eligibility_status"] == "READY"
                       and r["filing_date"] <= "2017-11-10"
                       and r["accession_number"] not in excluded),
                      key=lambda r: (r["filing_date"], r["accession_number"]))
        if len(pool) < quota:
            raise ValueError(f"Not enough READY {ticker} filings for first batch")
        indexes = {round(i * (len(pool)-1) / (quota-1)) for i in range(quota)}
        if len(indexes) != quota:
            raise ValueError("Quantile selection collided")
        selected.extend(pool[i] for i in sorted(indexes))
    selected.sort(key=lambda r: (r["ticker"], r["filing_date"], r["accession_number"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader(); writer.writerows(selected)
    return {"rows": len(selected), "ticker": dict(Counter(r["ticker"] for r in selected)),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "output": str(output)}


def validate_batch_rows(rows: list[dict]) -> None:
    if not (1 <= len(rows) <= MAX_BATCH_FILINGS) or len({r["accession_number"] for r in rows}) != len(rows):
        raise ValueError("Batch must have at most 100 distinct accessions")
    for row in rows:
        ticker = row.get("ticker", "")
        if ticker not in APPROVED:
            raise ValueError("Unknown ticker")
        issuer, mapping, start = APPROVED[ticker]
        if (row.get("eligibility_status") != "READY" or row.get("issuer_cik") != issuer or
                row.get("historical_security_id") != mapping or row.get("mapping_id") != mapping or
                row.get("approved_valid_from") != start or row.get("approved_valid_to") != "2017-11-10" or
                not start <= row.get("first_transaction_date", "") <= row.get("last_transaction_date", "") <= "2017-11-10"):
            raise ValueError("Historical security identity or interval mismatch")
        if row.get("document_type") not in {"4", "4/A", "5", "5/A"} or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row.get("filing_date", "")):
            raise ValueError("Invalid filing metadata")
        owners = row.get("reporting_owner_ciks", "").split("|")
        if any(not re.fullmatch(r"\d{10}", cik) for cik in owners) or row.get("archive_cik_candidate") not in {str(int(x)) for x in owners}:
            raise ValueError("Unresolved reporting-owner/archive identity")
        archive_url(row)


def open_batch_db(path: Path, manifest_sha: str) -> sqlite3.Connection:
    if path.name.lower() == "insider_research.db":
        raise ValueError("Cannot write to production database")
    if path.exists():
        probe = readonly_db(path)
        try:
            tables = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"batch_config", "http_attempts", "evidence", "sqlite_sequence"}:
                raise ValueError("Refusing non-batch existing database")
            if tables and "batch_config" not in tables:
                raise ValueError("Refusing an existing database without batch identity")
        finally:
            probe.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("BEGIN IMMEDIATE")
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS batch_config(manifest_sha256 TEXT PRIMARY KEY, max_filings INTEGER NOT NULL, max_attempts INTEGER NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS http_attempts(
          id INTEGER PRIMARY KEY, accession_number TEXT NOT NULL, attempt_number INTEGER NOT NULL,
          started_at_utc TEXT NOT NULL, finished_at_utc TEXT, exception_type TEXT,
          sanitized_error TEXT, http_status INTEGER, outcome TEXT NOT NULL,
          UNIQUE(accession_number,attempt_number))""")
        db.execute("""CREATE TABLE IF NOT EXISTS evidence(
          id INTEGER PRIMARY KEY, accession_number TEXT NOT NULL, source_url TEXT NOT NULL,
          retrieved_at_utc TEXT NOT NULL, response_sha256 TEXT, raw_response BLOB,
          raw_acceptance TEXT, parsed_utc TEXT, timezone_status TEXT NOT NULL,
          verification_status TEXT NOT NULL, detail TEXT NOT NULL, http_status INTEGER,
          attempt_count INTEGER NOT NULL)""")
        db.execute("CREATE TRIGGER IF NOT EXISTS batch_evidence_no_update BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT,'append-only'); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS batch_evidence_no_delete BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT,'append-only'); END")
        saved = db.execute("SELECT manifest_sha256,max_filings,max_attempts FROM batch_config").fetchall()
        if not saved:
            db.execute("INSERT INTO batch_config VALUES(?,?,?)", (manifest_sha, MAX_BATCH_FILINGS, MAX_BATCH_ATTEMPTS))
        elif saved != [(manifest_sha, MAX_BATCH_FILINGS, MAX_BATCH_ATTEMPTS)]:
            raise ValueError("Existing batch database manifest or budget mismatch")
        if db.execute("SELECT count(*) FROM http_attempts").fetchone()[0] > MAX_BATCH_ATTEMPTS:
            raise ValueError("Cumulative batch attempt budget exceeded")
    return db


def reserve(db: sqlite3.Connection, accession: str) -> int:
    db.execute("BEGIN IMMEDIATE")
    try:
        total = db.execute("SELECT count(*) FROM http_attempts").fetchone()[0]
        used = db.execute("SELECT count(*) FROM http_attempts WHERE accession_number=?", (accession,)).fetchone()[0]
        if total >= MAX_BATCH_ATTEMPTS or used >= MAX_FILING_ATTEMPTS:
            raise ValueError("Batch HTTP attempt budget exhausted")
        number = used + 1
        db.execute("INSERT INTO http_attempts(accession_number,attempt_number,started_at_utc,outcome) VALUES(?,?,?,?)",
                   (accession, number, datetime.now(timezone.utc).isoformat(), "IN_FLIGHT"))
        db.commit()
        return number
    except BaseException:
        db.rollback(); raise


def finish(db: sqlite3.Connection, accession: str, number: int, status: int | None,
           exception_type: str | None, message: object, outcome: str) -> None:
    with db:
        changed = db.execute("""UPDATE http_attempts SET finished_at_utc=?,http_status=?,exception_type=?,
                      sanitized_error=?,outcome=? WHERE accession_number=? AND attempt_number=? AND outcome='IN_FLIGHT'""",
                 (datetime.now(timezone.utc).isoformat(), status, exception_type,
                  sanitize_error(message) if message else None, outcome, accession, number)).rowcount
        if changed != 1:
            raise ValueError("Attempt reservation missing")


def recover_batch(manifest: Path, pilot_manifest: Path, batch_db: Path, expected_sha: str,
                  user_agent: str, opener=None, sleep=time.sleep, progress=print,
                  verified_evidence_dbs: list[Path] | None = None,
                  halt_on_429: bool = False) -> dict:
    if not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", user_agent):
        raise ValueError("Real contact email required in User-Agent")
    if opener is None:
        root = Path(__file__).resolve().parents[2]
        match = re.fullmatch(r"edgar_batch_(\d{3})\.db", batch_db.name)
        if (not match or batch_db.resolve() != (root / "data" / "processed" / batch_db.name).resolve() or
                not manifest.name.startswith(f"edgar_batch_{match.group(1)}_")):
            raise ValueError("Live batch must use the canonical isolated batch database")
        if match.group(1) != "001" and not verified_evidence_dbs:
            raise ValueError("Later batches require the prior evidence registry")
        if match.group(1) != "001":
            from src.market_data.edgar_remaining import EXPORTS, PROCESSED, load_plan
            plan, _ = load_plan()
            planned = next((item for item in plan["batches"] if item["batch"] == match.group(1)), None)
            if (planned is None or planned["sha256"] != expected_sha or
                    manifest.resolve() != (EXPORTS / planned["manifest"]).resolve()):
                raise ValueError("Later batch is not in the pinned remaining-accession plan")
            required_prior = {PROCESSED / "edgar_timestamp_pilot_20.db", PROCESSED / "edgar_batch_001.db"}
            required_prior.update(PROCESSED / f"edgar_batch_{item['batch']}.db" for item in plan["batches"]
                                  if item["batch"] != match.group(1) and
                                  (PROCESSED / f"edgar_batch_{item['batch']}.db").is_file())
            if {path.resolve() for path in verified_evidence_dbs} != {path.resolve() for path in required_prior}:
                raise ValueError("Later batch must inspect every existing prior evidence database")
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != expected_sha:
        raise ValueError("Batch manifest hash changed")
    if hashlib.sha256(pilot_manifest.read_bytes()).hexdigest() != PREFLIGHT_MANIFEST_SHA256:
        raise ValueError("Pilot exclusion manifest hash changed")
    with manifest.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with pilot_manifest.open(encoding="utf-8", newline="") as handle:
        pilot_accessions = {r["accession_number"] for r in csv.DictReader(handle)}
    validate_batch_rows(rows)
    if any(r["accession_number"] in pilot_accessions for r in rows):
        raise ValueError("Batch repeats pilot filing")
    if verified_evidence_dbs:
        prior_verified = set()
        for prior_path in verified_evidence_dbs:
            if not prior_path.is_file() or prior_path.resolve() == batch_db.resolve():
                raise ValueError("Missing or self-referential prior evidence database")
            prior_db = readonly_db(prior_path)
            try:
                prior_verified.update(r[0] for r in prior_db.execute(
                    "SELECT accession_number FROM evidence WHERE verification_status='VERIFIED_RAW'"))
            finally:
                prior_db.close()
        if any(r["accession_number"] in prior_verified for r in rows):
            raise ValueError("Batch repeats a previously verified filing")
    db = open_batch_db(batch_db, expected_sha)
    counts = defaultdict(int)
    sent_any = False
    try:
        for index, row in enumerate(rows, 1):
            accession = row["accession_number"]
            prior = db.execute("""SELECT verification_status,raw_response,response_sha256,raw_acceptance,
                                 source_url,http_status FROM evidence WHERE accession_number=? ORDER BY id DESC LIMIT 1""",
                               (accession,)).fetchone()
            if prior:
                state, prior_body, prior_sha, prior_raw, prior_url, prior_http = prior
                if state == "VERIFIED_RAW":
                    try:
                        if (prior_body is None or hashlib.sha256(prior_body).hexdigest() != prior_sha or
                                prior_url != archive_url(row) or verify_header(prior_body, row)[0] != prior_raw):
                            raise ValueError("Prior evidence failed integrity check")
                    except ValueError:
                        counts["HALTED_PRIOR_INTEGRITY"] = 1; break
                    counts["SKIPPED_VERIFIED"] += 1; continue
                if state in {"UNRESOLVED", "CONFLICT"}:
                    counts["SKIPPED_UNRESOLVED"] += 1; continue
                if prior_http in {401, 403, 429}:
                    counts["HALTED_PRIOR_POLICY"] = 1; break
            used = db.execute("SELECT count(*) FROM http_attempts WHERE accession_number=?", (accession,)).fetchone()[0]
            total = db.execute("SELECT count(*) FROM http_attempts").fetchone()[0]
            if used >= MAX_FILING_ATTEMPTS:
                counts["SKIPPED_EXHAUSTED"] += 1; continue
            if total >= MAX_BATCH_ATTEMPTS:
                counts["BUDGET_EXHAUSTED"] = 1; break
            if sent_any:
                sleep(PACE_SECONDS)
            else:
                last_started = db.execute("SELECT started_at_utc FROM http_attempts ORDER BY id DESC LIMIT 1").fetchone()
                if last_started:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_started[0])).total_seconds()
                    if elapsed < PACE_SECONDS:
                        sleep(PACE_SECONDS - max(elapsed, 0))
            numbered: dict[int, int] = {}
            def on_start(local: int) -> None:
                numbered[local] = reserve(db, accession)
            def on_result(local: int, status: int | None, exc_type: str | None,
                          message: object, outcome: str) -> None:
                finish(db, accession, numbered[local], status, exc_type, message, outcome)
            body, status, attempts, detail = fetch_bounded(archive_url(row), user_agent, opener=opener,
                sleep=sleep, max_attempts=min(MAX_FILING_ATTEMPTS-used, MAX_BATCH_ATTEMPTS-total),
                on_start=on_start, on_result=on_result, halt_on_429=halt_on_429)
            sent_any = True
            raw, state = None, "FAILED"
            if body is not None and status == 200:
                values = raw_acceptance_values(body)
                raw = values[0] if len(values) == 1 else json.dumps(values)
                try:
                    verified_raw, _ = verify_header(body, row)
                    old_values = {x[0] for x in db.execute("SELECT raw_acceptance FROM evidence WHERE accession_number=? AND verification_status='VERIFIED_RAW'", (accession,))}
                    if old_values and old_values != {verified_raw}:
                        state, detail = "CONFLICT", "acceptance_value_conflict"
                    else:
                        state, detail = "VERIFIED_RAW", "header_identity_and_raw_acceptance_verified;timezone_not_stored"
                except ValueError as exc:
                    state, detail = "UNRESOLVED", str(exc)
            elif status in {404, 410}:
                state = "UNRESOLVED"
            with db:
                db.execute("""INSERT INTO evidence(accession_number,source_url,retrieved_at_utc,response_sha256,
                    raw_response,raw_acceptance,parsed_utc,timezone_status,verification_status,detail,http_status,attempt_count)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (accession, archive_url(row), datetime.now(timezone.utc).isoformat(),
                    hashlib.sha256(body).hexdigest() if body is not None else None, body, raw, None,
                    "UNVERIFIED", state, detail, status, attempts))
            counts[state] += 1
            progress(json.dumps({"position": index, "accession": accession, "status": state,
                                 "attempts_cumulative": db.execute("SELECT count(*) FROM http_attempts").fetchone()[0],
                                 "budget": MAX_BATCH_ATTEMPTS}), flush=True)
            if state in {"FAILED", "CONFLICT"} or (state == "UNRESOLVED" and status == 200):
                counts["HALTED"] = 1; break
        counts["ATTEMPTS_CUMULATIVE"] = db.execute("SELECT count(*) FROM http_attempts").fetchone()[0]
        return dict(counts)
    finally:
        db.close()


def write_report(manifest: Path, batch_db: Path, output: Path) -> dict:
    with manifest.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    db = readonly_db(batch_db)
    try:
        status = {r[0]: r[1:] for r in db.execute("""SELECT accession_number,verification_status,detail,raw_acceptance,
                 source_url,response_sha256 FROM evidence ORDER BY id""")}
        attempts = {r[0]: r[1] for r in db.execute("SELECT accession_number,count(*) FROM http_attempts GROUP BY accession_number")}
    finally:
        db.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, ["accession_number", "ticker", "status", "detail", "raw_acceptance", "source_url", "response_sha256", "attempts"])
        writer.writeheader()
        for row in rows:
            state, detail, raw, url, digest = status.get(row["accession_number"], ("NOT_REQUESTED", "", "", "", ""))
            writer.writerow(dict(accession_number=row["accession_number"], ticker=row["ticker"], status=state,
                                 detail=detail, raw_acceptance=raw, source_url=url, response_sha256=digest,
                                 attempts=attempts.get(row["accession_number"], 0)))
    return dict(Counter(status.get(r["accession_number"], ("NOT_REQUESTED",))[0] for r in rows))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="Offline first-batch selection")
    prep.add_argument("--full-manifest", type=Path, required=True)
    prep.add_argument("--pilot-manifest", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    live = sub.add_parser("recover", help="LIVE batch; requires separate authorization")
    live.add_argument("--manifest", type=Path, required=True)
    live.add_argument("--pilot-manifest", type=Path, required=True)
    live.add_argument("--batch-db", type=Path, required=True)
    live.add_argument("--expected-sha256", required=True)
    live.add_argument("--user-agent", required=True)
    live.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare_first_batch(args.full_manifest, args.pilot_manifest, args.output), indent=2))
    else:
        try:
            result = recover_batch(args.manifest, args.pilot_manifest, args.batch_db, args.expected_sha256, args.user_agent)
        finally:
            if args.batch_db.is_file():
                report_status = write_report(args.manifest, args.batch_db, args.report)
        result["report_status"] = report_status
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
