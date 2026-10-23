"""Offline remaining-accession plan and one-batch-at-a-time EDGAR recovery.

Only ``recover`` can use the network. ``prepare`` and ``status`` read local
manifests/evidence and write reports; neither opens the production database.
"""

from __future__ import annotations
import sqlite3

import argparse
import csv
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.market_data.edgar_batch import MAX_BATCH_ATTEMPTS, MAX_BATCH_FILINGS, PACE_SECONDS, recover_batch, validate_batch_rows, write_report
from src.market_data.edgar_full_manifest import FIELDS
from src.market_data.edgar_timestamp_pilot import (
    PREFLIGHT_MANIFEST_SHA256, archive_url, fetch_bounded, raw_acceptance_values,
    readonly_db, sanitize_error, verify_header,
)

ROOT = Path(__file__).resolve().parents[2]
EXPORTS = ROOT / "data" / "exports" / "market_data"
PROCESSED = ROOT / "data" / "processed"
FULL = EXPORTS / "edgar_full_6067_manifest.csv"
PILOT = EXPORTS / "edgar_timestamp_20_manifest.csv"
BATCH001 = EXPORTS / "edgar_batch_001_100_manifest.csv"
PLAN = EXPORTS / "edgar_remaining_plan.json"
REMAINING = EXPORTS / "edgar_remaining_standard_manifest.csv"
RETRY = EXPORTS / "edgar_exhausted_pilot_controlled_retry.csv"
COVERAGE = EXPORTS / "edgar_remaining_coverage.csv"
ACCESSION_STATUS = EXPORTS / "edgar_remaining_accession_status.csv"
FULL_SHA256 = "c645ea536de65448825bd210ab8720c412611e6434caf999eecc682db3d8c164"
BATCH001_SHA256 = "92e3e268f9ed67f85b32a0eb2b71a8445642a74fe204aca0a1f470a6083fa57c"
EXHAUSTED_ACCESSION = "0001305763-06-000002"
CONTROLLED_DB = PROCESSED / "edgar_controlled_retry.db"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_hash(path: Path, expected: str) -> None:
    if digest(path) != expected:
        raise ValueError(f"Pinned input hash mismatch: {path}")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != FIELDS:
            raise ValueError(f"Unexpected manifest columns: {path}")
        rows = list(reader)
    if not rows or len({r["accession_number"] for r in rows}) != len(rows):
        raise ValueError(f"Empty or duplicate accession manifest: {path}")
    for start in range(0, len(rows), MAX_BATCH_FILINGS):
        validate_batch_rows(rows[start:start + MAX_BATCH_FILINGS])
    return rows


def pinned_sources() -> tuple[list[dict[str, str]], set[str], set[str]]:
    require_hash(FULL, FULL_SHA256)
    require_hash(PILOT, PREFLIGHT_MANIFEST_SHA256)
    require_hash(BATCH001, BATCH001_SHA256)
    full = read_rows(FULL)
    if len(full) != 6067:
        raise ValueError("Full accession manifest must contain 6,067 rows")
    full_by_acc = {r["accession_number"]: r for r in full}
    with PILOT.open(encoding="utf-8", newline="") as handle:
        pilot = {r["accession_number"] for r in csv.DictReader(handle)}
    first = read_rows(BATCH001)
    if len(pilot) != 20 or len(first) != 100 or pilot & {r["accession_number"] for r in first}:
        raise ValueError("Pilot/Batch 001 scope mismatch")
    if any(full_by_acc.get(r["accession_number"]) != r for r in first) or not pilot <= full_by_acc.keys():
        raise ValueError("Pilot/Batch 001 accession identity diverges from full manifest")
    return full, pilot, {r["accession_number"] for r in first}


def evidence_state(full: list[dict[str, str]], plan: dict | None = None) -> tuple[dict[str, dict], list[Path]]:
    """Validate all extant evidence, fail on overlap/conflict, keep raw clock unconverted."""
    by_acc = {r["accession_number"]: r for r in full}
    paths = [PROCESSED / "edgar_timestamp_pilot_20.db", PROCESSED / "edgar_batch_001.db"]
    if plan:
        paths += [PROCESSED / f"edgar_batch_{item['batch']}.db" for item in plan["batches"]
                   if (PROCESSED / f"edgar_batch_{item['batch']}.db").exists()]
    if CONTROLLED_DB.is_file():
        paths.append(CONTROLLED_DB)
    if not all(p.is_file() for p in paths[:2]):
        raise FileNotFoundError("Pilot and Batch 001 evidence databases are required")
    found: dict[str, dict] = {}
    for path in paths:
        db = readonly_db(path)
        try:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError(f"SQLite integrity failed: {path}")
            if path.name.startswith("edgar_batch_"):
                config = db.execute("SELECT manifest_sha256,max_filings,max_attempts FROM batch_config").fetchall()
                expected = BATCH001_SHA256 if path.name == "edgar_batch_001.db" else next(
                    x["sha256"] for x in plan["batches"] if f"edgar_batch_{x['batch']}.db" == path.name)
                if config != [(expected, MAX_BATCH_FILINGS, MAX_BATCH_ATTEMPTS)]:
                    raise ValueError(f"Batch identity or budget changed: {path}")
            if path == CONTROLLED_DB:
                allowed = {EXHAUSTED_ACCESSION}
            elif path.name == "edgar_timestamp_pilot_20.db":
                with PILOT.open(encoding="utf-8", newline="") as handle:
                    allowed = {r["accession_number"] for r in csv.DictReader(handle)}
            elif path.name == "edgar_batch_001.db":
                allowed = {r["accession_number"] for r in read_rows(BATCH001)}
            else:
                batch = path.stem.rsplit("_", 1)[-1]
                item = next(x for x in plan["batches"] if x["batch"] == batch)
                allowed = {r["accession_number"] for r in read_rows(EXPORTS / item["manifest"])}
            attempts = dict(db.execute("SELECT accession_number,count(*) FROM http_attempts GROUP BY accession_number"))
            if path.name.startswith("edgar_batch_") and sum(attempts.values()) > MAX_BATCH_ATTEMPTS:
                raise ValueError(f"Batch attempt budget exceeded: {path}")
            for accession, count in attempts.items():
                limit = 1 if path == CONTROLLED_DB else 3
                if accession not in allowed or count > limit:
                    raise ValueError(f"Unexpected/excess attempt: {accession} in {path}")
            for acc, state, body, sha, raw, url, http, parsed, timezone_status in db.execute(
                    "SELECT accession_number,verification_status,raw_response,response_sha256,raw_acceptance,"
                    "source_url,http_status,parsed_utc,timezone_status FROM evidence ORDER BY id"):
                controlled_transition = (path == CONTROLLED_DB and acc == EXHAUSTED_ACCESSION and
                                         found.get(acc, {}).get("status") == "FAILED")
                if acc not in allowed or (acc in found and found[acc]["database"] != path.name and not controlled_transition):
                    raise ValueError(f"Unexpected or repeated evidence accession: {acc}")
                row = by_acc[acc]
                if url != archive_url(row):
                    raise ValueError(f"Evidence source URL mismatch: {acc}")
                if body is not None and hashlib.sha256(body).hexdigest() != sha:
                    raise ValueError(f"Raw evidence hash mismatch: {acc}")
                if state == "VERIFIED_RAW":
                    if body is None or http != 200 or verify_header(body, row)[0] != raw:
                        raise ValueError(f"Verified filing failed identity check: {acc}")
                    if parsed is not None or timezone_status != "UNVERIFIED":
                        raise ValueError(f"Unverified timezone must not be promoted: {acc}")
                if acc in found and found[acc]["status"] == "VERIFIED_RAW":
                    raise ValueError(f"Evidence appended after verified state: {acc}")
                found[acc] = {"status": state, "database": path.name, "attempts": attempts.get(acc, 0),
                              "raw_acceptance": raw or "", "response_sha256": sha or ""}
            for acc, count in attempts.items():
                if acc not in found:
                    found[acc] = {"status": "ATTEMPTED_NO_EVIDENCE", "database": path.name,
                                  "attempts": count, "raw_acceptance": "", "response_sha256": ""}
        finally:
            db.close()
    return found, paths


def write_csv_immutable(path: Path, rows: list[dict[str, str]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    from io import StringIO
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, FIELDS, lineterminator="\r\n")
    writer.writeheader(); writer.writerows(rows)
    payload = buffer.getvalue().encode("utf-8")
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"Refusing to overwrite a changed manifest: {path}")
    if not path.exists():
        path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def partition_remaining(full: list[dict[str, str]], verified: set[str], exhausted: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    all_accessions = {r["accession_number"] for r in full}
    if len(all_accessions) != len(full) or not verified <= all_accessions or exhausted not in all_accessions or exhausted in verified:
        raise ValueError("Invalid accession partition inputs")
    standard = [r for r in full if r["accession_number"] not in verified and r["accession_number"] != exhausted]
    retry = [r for r in full if r["accession_number"] == exhausted]
    if len(standard) + len(retry) + len(verified) != len(full):
        raise ValueError("Accession partition does not reconcile")
    return standard, retry


def prepare() -> dict:
    full, pilot, first = pinned_sources()
    found, _ = evidence_state(full)
    verified = {acc for acc, e in found.items() if e["status"] == "VERIFIED_RAW"}
    if len(verified) != 119 or not verified <= pilot | first:
        raise ValueError("Expected exactly 119 verified pilot/Batch 001 filings")
    if found.get(EXHAUSTED_ACCESSION, {}).get("status") != "FAILED" or found[EXHAUSTED_ACCESSION]["attempts"] != 3:
        raise ValueError("Exhausted pilot accession must remain unresolved with three attempts")
    if any(acc in first and found.get(acc, {}).get("status") != "VERIFIED_RAW" for acc in first):
        raise ValueError("Batch 001 is not 100/100 verified")
    standard, retry = partition_remaining(full, verified, EXHAUSTED_ACCESSION)
    if (len(standard), len(retry)) != (5947, 1):
        raise ValueError("Remaining accession accounting failed")
    standard_sha = write_csv_immutable(REMAINING, standard)
    retry_sha = write_csv_immutable(RETRY, retry)
    batches = []
    for offset in range(0, len(standard), MAX_BATCH_FILINGS):
        number = f"{offset // MAX_BATCH_FILINGS + 2:03d}"
        batch_rows = standard[offset:offset + MAX_BATCH_FILINGS]
        path = EXPORTS / f"edgar_batch_{number}_{len(batch_rows):03d}_manifest.csv"
        batches.append({"batch": number, "manifest": path.name, "rows": len(batch_rows),
                        "sha256": write_csv_immutable(path, batch_rows)})
    plan = {"schema_version": 1, "full_sha256": FULL_SHA256, "pilot_sha256": PREFLIGHT_MANIFEST_SHA256,
            "batch001_sha256": BATCH001_SHA256, "standard_manifest": REMAINING.name,
            "standard_sha256": standard_sha, "retry_manifest": RETRY.name, "retry_sha256": retry_sha,
            "retry_accession": EXHAUSTED_ACCESSION, "retry_policy": "SEPARATE_OWNER_AUTHORIZATION_REQUIRED",
            "batches": batches}
    payload = (json.dumps(plan, indent=2) + "\n").encode("utf-8")
    if PLAN.exists() and PLAN.read_bytes() != payload:
        raise ValueError("Refusing to overwrite a changed remaining-accession plan")
    if not PLAN.exists():
        PLAN.write_bytes(payload)
    coverage = status(plan)
    return {"verified_reused": 119, "standard_queued": 5947, "controlled_retry": 1,
            "batches": len(batches), "first_batch_sha256": batches[0]["sha256"], "coverage": coverage}


def load_plan() -> tuple[dict, list[dict[str, str]]]:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    full, _, _ = pinned_sources()
    if plan.get("schema_version") != 1 or plan.get("full_sha256") != FULL_SHA256 or len(plan.get("batches", [])) != 60:
        raise ValueError("Unexpected remaining-accession plan")
    for name, sha in ((plan["standard_manifest"], plan["standard_sha256"]),
                      (plan["retry_manifest"], plan["retry_sha256"])):
        require_hash(EXPORTS / name, sha)
    standard = read_rows(EXPORTS / plan["standard_manifest"])
    retry = read_rows(EXPORTS / plan["retry_manifest"])
    if len(standard) != 5947 or len(retry) != 1 or retry[0]["accession_number"] != EXHAUSTED_ACCESSION:
        raise ValueError("Remaining queue counts changed")
    planned = []
    for item in plan["batches"]:
        if not re.fullmatch(r"\d{3}", item["batch"]):
            raise ValueError("Invalid batch number")
        path = EXPORTS / item["manifest"]
        require_hash(path, item["sha256"])
        rows = read_rows(path)
        if len(rows) != item["rows"]:
            raise ValueError("Batch row count changed")
        planned.extend(rows)
    if planned != standard or len({r["accession_number"] for r in planned + retry}) != 5948:
        raise ValueError("Batch partition does not match the remaining queue")
    by_acc = {r["accession_number"]: r for r in full}
    if any(by_acc.get(r["accession_number"]) != r for r in planned + retry):
        raise ValueError("Remaining queue identity differs from pinned full manifest")
    return plan, full


def status(plan: dict | None = None) -> dict:
    if plan is None:
        plan, full = load_plan()
    else:
        full, _, _ = pinned_sources()
    found, _ = evidence_state(full, plan)
    queued = {r["accession_number"]: item["batch"] for item in plan["batches"]
              for r in read_rows(EXPORTS / item["manifest"])}
    detail = []
    grouped = defaultdict(lambda: {"accessions": 0, "local_transactions": 0})
    for row in full:
        acc = row["accession_number"]
        evidence = found.get(acc)
        state = evidence["status"] if evidence else ("CONTROLLED_RETRY" if acc == EXHAUSTED_ACCESSION else "QUEUED")
        if acc == EXHAUSTED_ACCESSION and state != "VERIFIED_RAW":
            state = "CONTROLLED_RETRY"
        batch = evidence["database"] if evidence else queued.get(acc, "")
        item = {"accession_number": acc, "ticker": row["ticker"], "historical_security_id": row["historical_security_id"],
                "filing_year": row["filing_date"][:4], "document_type": row["document_type"],
                "filing_date": row["filing_date"], "status": state, "batch_or_database": batch,
                "http_attempts": evidence["attempts"] if evidence else 0,
                "local_transaction_count": int(row["local_transaction_count"])}
        detail.append(item)
        group = grouped[(item["ticker"], item["filing_year"], item["document_type"], state)]
        group["accessions"] += 1
        group["local_transactions"] += item["local_transaction_count"]
    for path, rows, fields in ((ACCESSION_STATUS, detail, list(detail[0])),
                               (COVERAGE, [dict(ticker=k[0], filing_year=k[1], document_type=k[2], status=k[3], **v)
                                           for k, v in sorted(grouped.items())],
                                ["ticker", "filing_year", "document_type", "status", "accessions", "local_transactions"])):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader(); writer.writerows(rows)
    return {"status": dict(Counter(x["status"] for x in detail)), "accessions": len(detail),
            "transactions": sum(x["local_transaction_count"] for x in detail),
            "coverage_rows": len(grouped), "coverage_csv": str(COVERAGE)}


def recover_planned(number: str, user_agent: str, opener=None, sleep=None, progress=print,
                    halt_on_429: bool = False) -> dict:
    plan, full = load_plan()
    item = next((x for x in plan["batches"] if x["batch"] == number), None)
    if item is None:
        raise ValueError("Batch not in pinned plan; controlled retry is excluded")
    found, paths = evidence_state(full, plan)
    manifest = EXPORTS / item["manifest"]
    rows = read_rows(manifest)
    db_path = PROCESSED / f"edgar_batch_{number}.db"
    if any(found.get(r["accession_number"], {}).get("status") == "VERIFIED_RAW" and
           found[r["accession_number"]]["database"] != db_path.name for r in rows):
        raise ValueError("Planned batch overlaps verified evidence; no repeat download allowed")
    prior_paths = [p for p in paths if p != db_path]
    sleeper = sleep or time.sleep
    last_attempts = []
    for path in paths:
        prior_db = readonly_db(path)
        try:
            last = prior_db.execute("SELECT started_at_utc FROM http_attempts ORDER BY id DESC LIMIT 1").fetchone()
            if last:
                last_attempts.append(datetime.fromisoformat(last[0]))
        finally:
            prior_db.close()
    if last_attempts:
        gap = (datetime.now(timezone.utc) - max(last_attempts)).total_seconds()
        if gap < PACE_SECONDS:
            sleeper(PACE_SECONDS - max(gap, 0))
    kwargs = {"verified_evidence_dbs": prior_paths, "opener": opener, "progress": progress,
              "halt_on_429": halt_on_429}
    if sleep is not None:
        kwargs["sleep"] = sleeper
    try:
        result = recover_batch(manifest, PILOT, db_path, item["sha256"], user_agent, **kwargs)
    finally:
        if db_path.is_file():
            write_report(manifest, db_path, EXPORTS / f"edgar_batch_{number}_validation.csv")
            status(plan)
    return result


def recover_controlled(accession: str, user_agent: str, opener=None, sleep=None) -> dict:
    """LIVE, separately authorized, exactly one fresh attempt for the pinned retry row."""
    if accession != EXHAUSTED_ACCESSION:
        raise ValueError("Only the pinned controlled-retry accession is permitted")
    if not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", user_agent):
        raise ValueError("Real contact email required in User-Agent")
    plan, full = load_plan()
    retry_rows = read_rows(RETRY)
    if len(retry_rows) != 1 or retry_rows[0]["accession_number"] != accession:
        raise ValueError("Controlled-retry manifest identity changed")
    found, _ = evidence_state(full, plan)
    prior = found.get(accession)
    if prior is None or prior["status"] != "FAILED" or prior["attempts"] != 3:
        raise ValueError("Controlled retry requires the pinned three-attempt pilot failure")

    CONTROLLED_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(CONTROLLED_DB)
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS http_attempts(
              id INTEGER PRIMARY KEY, accession_number TEXT NOT NULL UNIQUE,
              started_at_utc TEXT NOT NULL, finished_at_utc TEXT, exception_type TEXT,
              sanitized_error TEXT, http_status INTEGER, outcome TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS evidence(
              id INTEGER PRIMARY KEY, accession_number TEXT NOT NULL UNIQUE, source_url TEXT NOT NULL,
              retrieved_at_utc TEXT NOT NULL, response_sha256 TEXT, raw_response BLOB,
              raw_acceptance TEXT, parsed_utc TEXT, timezone_status TEXT NOT NULL,
              verification_status TEXT NOT NULL, detail TEXT NOT NULL, http_status INTEGER,
              attempt_count INTEGER NOT NULL)""")
            db.execute("CREATE TRIGGER IF NOT EXISTS controlled_no_update BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT,'append-only'); END")
            db.execute("CREATE TRIGGER IF NOT EXISTS controlled_no_delete BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT,'append-only'); END")
        existing = db.execute("SELECT outcome FROM http_attempts WHERE accession_number=?", (accession,)).fetchone()
        if existing:
            raise ValueError(f"Controlled retry budget already consumed: {existing[0]}")
        started = datetime.now(timezone.utc).isoformat()
        with db:
            db.execute("INSERT INTO http_attempts(accession_number,started_at_utc,outcome) VALUES(?,?,?)",
                       (accession, started, "IN_FLIGHT"))
        row = retry_rows[0]
        result: dict[str, object] = {}

        def on_result(_local: int, status: int | None, exc_type: str | None,
                      message: object, outcome: str) -> None:
            with db:
                db.execute("""UPDATE http_attempts SET finished_at_utc=?,exception_type=?,sanitized_error=?,
                              http_status=?,outcome=? WHERE accession_number=? AND outcome='IN_FLIGHT'""",
                           (datetime.now(timezone.utc).isoformat(), exc_type,
                            sanitize_error(message) if message else None, status, outcome, accession))

        body, status, attempts, detail = fetch_bounded(
            archive_url(row), user_agent, opener=opener, sleep=sleep or time.sleep,
            max_attempts=1, on_start=lambda _local: None, on_result=on_result, halt_on_429=True)
        raw, verification = None, "FAILED"
        if body is not None and status == 200:
            values = raw_acceptance_values(body)
            raw = values[0] if len(values) == 1 else json.dumps(values)
            try:
                raw, _ = verify_header(body, row)
                verification, detail = "VERIFIED_RAW", "header_identity_and_raw_acceptance_verified;timezone_not_stored"
            except ValueError as exc:
                verification, detail = "UNRESOLVED", str(exc)
        elif status in {404, 410}:
            verification = "UNRESOLVED"
        with db:
            db.execute("""INSERT INTO evidence(accession_number,source_url,retrieved_at_utc,response_sha256,
              raw_response,raw_acceptance,parsed_utc,timezone_status,verification_status,detail,http_status,attempt_count)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (accession, archive_url(row), datetime.now(timezone.utc).isoformat(),
              hashlib.sha256(body).hexdigest() if body is not None else None, body, raw, None, "UNVERIFIED",
              verification, detail, status, attempts))
        result.update(status=verification, http_status=status, attempts=attempts, detail=detail)
        return result
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="Offline: pin all remaining accession/batch manifests")
    sub.add_parser("status", help="Offline: validate evidence and refresh consolidated coverage")
    live = sub.add_parser("recover", help="LIVE: one separately authorized batch, at most 100 filings")
    live.add_argument("--batch", required=True, help="Three-digit batch number, e.g. 002")
    live.add_argument("--user-agent", required=True)
    retry = sub.add_parser("controlled-retry", help="LIVE: one authorized attempt for the pinned exhausted accession")
    retry.add_argument("--accession", required=True)
    retry.add_argument("--user-agent", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare()
    elif args.command == "status":
        result = status()
    elif args.command == "recover":
        result = recover_planned(args.batch, args.user_agent)
    else:
        result = recover_controlled(args.accession, args.user_agent)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()


