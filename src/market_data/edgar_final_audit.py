"""Offline final integrity audit for the pinned 6,067-filing EDGAR cohort.

This command never opens the network and never mutates the SQLite evidence
databases.  Raw filings remain stored as BLOBs in their append-only evidence DB.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.market_data import edgar_remaining
from src.market_data.edgar_timestamp_pilot import archive_url, readonly_db, verify_header

ROOT = Path(__file__).resolve().parents[2]
EXPORTS = ROOT / "data" / "exports" / "market_data"
SUMMARY = EXPORTS / "edgar_final_audit_summary.json"
DISCREPANCIES = EXPORTS / "edgar_final_audit_discrepancies.csv"
METADATA = EXPORTS / "edgar_filing_event_metadata.csv"

DISCREPANCY_FIELDS = ["accession", "check_name", "severity", "evidence", "recommended_resolution"]
METADATA_FIELDS = [
    "accession_number", "issuer_cik", "reporting_owner_cik", "form_type", "filing_date",
    "acceptance_datetime_raw", "acceptance_datetime_utc", "acceptance_datetime_new_york",
    "source_url", "raw_sha256", "verification_status",
]


def convert_acceptance(raw: str) -> tuple[str, str]:
    """Interpret EDGAR's raw acceptance wall clock as America/New_York.

    Ambiguous/nonexistent DST wall times are rejected rather than inferred.
    """
    wall = datetime.strptime(raw, "%Y%m%d%H%M%S")
    zone = ZoneInfo("America/New_York")
    first = wall.replace(tzinfo=zone, fold=0)
    second = wall.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("ambiguous America/New_York wall clock")
    utc = first.astimezone(timezone.utc)
    if utc.astimezone(zone).replace(tzinfo=None) != wall:
        raise ValueError("nonexistent America/New_York wall clock")
    return utc.isoformat(), first.isoformat()


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def evidence_paths(plan: dict) -> list[Path]:
    paths = [
        edgar_remaining.PROCESSED / "edgar_timestamp_pilot_20.db",
        edgar_remaining.PROCESSED / "edgar_batch_001.db",
    ]
    paths.extend(edgar_remaining.PROCESSED / f"edgar_batch_{item['batch']}.db" for item in plan["batches"])
    if edgar_remaining.CONTROLLED_DB.is_file():
        paths.append(edgar_remaining.CONTROLLED_DB)
    return paths


def authorized_controlled_transition(accession: str, row: dict[str, str], history: list[dict]) -> tuple[bool, str]:
    """Recognize only the pinned FAILED -> VERIFIED_RAW recovery chain."""
    if accession != edgar_remaining.EXHAUSTED_ACCESSION or len(history) != 2:
        return False, "not the unique pinned two-record controlled transition"
    historical, retry = history
    if (historical["database"] != "edgar_timestamp_pilot_20.db" or historical["status"] != "FAILED"
            or historical["attempts"] != 3):
        return False, "historical evidence is not the pinned exhausted pilot failure"
    if (retry["database"] != edgar_remaining.CONTROLLED_DB.name or retry["status"] != "VERIFIED_RAW"
            or retry["attempts"] != 1 or retry["http_status"] != 200):
        return False, "controlled evidence is not a one-attempt HTTP 200 VERIFIED_RAW result"
    body = retry["body"]
    if body is None or retry["source_url"] != archive_url(row):
        return False, "controlled raw response or canonical source URL is missing"
    if hashlib.sha256(body).hexdigest() != retry["raw_sha256"]:
        return False, "controlled raw SHA-256 does not match"
    try:
        raw, _ = verify_header(body, row)
    except ValueError as exc:
        return False, f"controlled SEC header identity failed: {exc}"
    if raw != retry["raw_acceptance"]:
        return False, "controlled raw acceptance value does not match the verified header"
    return True, "authorized pinned FAILED -> VERIFIED_RAW controlled retry"


def checkpoint_reconciles(checkpoint: dict, plan_sha256: str, manifest_count: int,
                          verified: int, total_attempts: int,
                          controlled_successes: int, controlled_attempts: int) -> bool:
    """Reconcile immutable sequential history plus the separately authorized delta."""
    return (
        checkpoint.get("plan_sha256") == plan_sha256
        and checkpoint.get("verified_total") == verified - controlled_successes
        and checkpoint.get("controlled_retry") == 1
        and manifest_count - checkpoint.get("verified_total", -1) == 1
        and checkpoint.get("remaining_normal") == 0
        and checkpoint.get("state") == "COMPLETED"
        and checkpoint.get("next_batch") is None
        and checkpoint.get("http_attempts_total") == total_attempts - controlled_attempts
    )


def audit() -> dict:
    plan, manifest = edgar_remaining.load_plan()
    manifest_by_accession = {row["accession_number"]: row for row in manifest}
    if len(manifest) != 6067 or len(manifest_by_accession) != 6067:
        raise ValueError("Pinned manifest is not exactly 6,067 unique accessions")

    # Reuse the strict repository verifier first. It checks SQLite integrity,
    # source URLs, raw hashes, exact SEC header identity, and cross-DB overlap.
    state, paths = edgar_remaining.evidence_state(manifest, plan)
    expected_paths = evidence_paths(plan)
    if [path.resolve() for path in paths] != [path.resolve() for path in expected_paths]:
        raise ValueError("Evidence registry does not include the complete pinned batch history")

    discrepancies: list[dict[str, str]] = []
    records: dict[str, dict] = {}
    seen_evidence = Counter()
    histories: dict[str, list[dict]] = {}
    seen_attempts = Counter()
    checks = Counter()

    def discrepancy(accession: str, check: str, severity: str, evidence: str, resolution: str) -> None:
        discrepancies.append({"accession": accession, "check_name": check, "severity": severity,
                              "evidence": evidence, "recommended_resolution": resolution})
        checks[f"{check}:failed"] += 1

    for path in paths:
        db = readonly_db(path)
        try:
            checks["sqlite_integrity:passed"] += 1
            for accession, count in db.execute(
                    "SELECT accession_number,count(*) FROM http_attempts GROUP BY accession_number"):
                seen_attempts[accession] += count
            database_attempts = dict(db.execute(
                "SELECT accession_number,count(*) FROM http_attempts GROUP BY accession_number"))
            query = """SELECT accession_number,verification_status,source_url,response_sha256,raw_response,
                              raw_acceptance,http_status,detail,retrieved_at_utc
                       FROM evidence ORDER BY id"""
            for accession, status, source_url, digest, body, raw_acceptance, http_status, detail, retrieved in db.execute(query):
                seen_evidence[accession] += 1
                record = {"status": status, "source_url": source_url or "", "raw_sha256": digest or "",
                                      "body": body, "raw_acceptance": raw_acceptance or "", "http_status": http_status,
                                      "detail": detail, "database": path.name, "retrieved_at_utc": retrieved,
                                      "attempts": database_attempts.get(accession, 0)}
                records[accession] = record
                histories.setdefault(accession, []).append(record)
        finally:
            db.close()

    cohort_counts = Counter(row["ticker"] for row in manifest)
    checks["manifest_unique:passed"] = len(manifest)
    checks["manifest_cohort:passed"] = len(manifest)
    metadata: list[dict[str, str]] = []
    verified = 0
    timestamp_converted = 0
    timestamp_not_verifiable = 0

    for row in manifest:
        accession = row["accession_number"]
        record = records.get(accession)
        final_status = "CONTROLLED_RETRY" if accession == edgar_remaining.EXHAUSTED_ACCESSION and (
            record is None or record["status"] != "VERIFIED_RAW") else (record["status"] if record else "MISSING")
        raw, utc_value, ny_value, source_url, digest = "", "", "", archive_url(row), ""

        if final_status == "VERIFIED_RAW" and record:
            verified += 1
            body = record["body"]
            digest = record["raw_sha256"]
            source_url = record["source_url"]
            checks["raw_document_present:passed"] += 1
            checks["raw_sha256:passed"] += 1
            checks["header_identity:passed"] += 1
            checks["source_url:passed"] += 1
            raw = record["raw_acceptance"]
            try:
                utc_value, ny_value = convert_acceptance(raw)
                timestamp_converted += 1
                checks["acceptance_datetime:passed"] += 1
            except ValueError as exc:
                timestamp_not_verifiable += 1
                discrepancy(accession, "acceptance_datetime", "WARNING", f"{raw}: {exc}",
                            "Review the raw SEC header; do not infer a UTC instant.")
            # Explicit recomputation keeps this export defensible even if the
            # shared verifier changes later.
            if body is None or hashlib.sha256(body).hexdigest() != digest:
                discrepancy(accession, "raw_sha256", "CRITICAL", record["database"],
                            "Restore the immutable evidence DB from a verified backup.")
            else:
                verified_raw, _ = verify_header(body, row)
                if verified_raw != raw:
                    discrepancy(accession, "header_identity", "CRITICAL", record["database"],
                                "Quarantine the evidence record and investigate source identity.")
        else:
            timestamp_not_verifiable += 1
            discrepancy(accession, "final_verification", "BLOCKER",
                        f"status={final_status}; attempts={seen_attempts.get(accession, 0)}; "
                        f"detail={(record or {}).get('detail', 'no evidence')}",
                        "Use the separately authorized, bounded controlled-retry procedure; retain unresolved status until verified source evidence exists.")
            checks["raw_document_present:not_verifiable"] += 1
            checks["raw_sha256:not_verifiable"] += 1
            checks["header_identity:not_verifiable"] += 1
            checks["acceptance_datetime:not_verifiable"] += 1

        owners = row["reporting_owner_ciks"].split("|")
        metadata.append({
            "accession_number": accession,
            "issuer_cik": row["issuer_cik"],
            "reporting_owner_cik": "|".join(owners),
            "form_type": row["document_type"],
            "filing_date": row["filing_date"],
            "acceptance_datetime_raw": raw,
            "acceptance_datetime_utc": utc_value,
            "acceptance_datetime_new_york": ny_value,
            "source_url": source_url,
            "raw_sha256": digest,
            "verification_status": final_status,
        })

    orphans = sorted(set(records) - set(manifest_by_accession))
    for accession in orphans:
        discrepancy(accession, "orphan_evidence", "CRITICAL", records[accession]["database"],
                    "Quarantine and investigate evidence outside the pinned cohort.")
    authorized_transitions = set()
    for accession, history in histories.items():
        if len(history) > 1:
            valid, _reason = authorized_controlled_transition(accession, manifest_by_accession.get(accession, {}), history)
            if valid:
                authorized_transitions.add(accession)
                checks["authorized_controlled_transition:passed"] += 1
    contradictory = sorted(accession for accession, count in seen_evidence.items()
                           if count > 1 and accession not in authorized_transitions)
    if contradictory:
        # evidence_state normally raises first; retain this for independently testable audit behavior.
        for accession in contradictory:
            discrepancy(accession, "multiple_final_evidence", "CRITICAL", f"rows={seen_evidence[accession]}",
                        "Resolve append-only evidence history without deleting source records.")

    checkpoint = json.loads((EXPORTS / "edgar_sequential_progress.json").read_text(encoding="utf-8"))
    plan_sha256 = hashlib.sha256(edgar_remaining.PLAN.read_bytes()).hexdigest()
    controlled_attempts = sum(item["attempts"] for item in histories.get(edgar_remaining.EXHAUSTED_ACCESSION, [])
                              if item["database"] == edgar_remaining.CONTROLLED_DB.name)
    checkpoint_consistent = checkpoint_reconciles(
        checkpoint, plan_sha256, len(manifest), verified, sum(seen_attempts.values()),
        len(authorized_transitions), controlled_attempts)
    if not checkpoint_consistent:
        discrepancy("", "checkpoint_consistency", "CRITICAL", "Sequential checkpoint does not reconcile",
                    "Investigate the immutable sequential checkpoint and controlled-retry delta; do not rewrite evidence or the pinned plan.")
    else:
        checks["checkpoint_consistency:passed"] += 1

    summary = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "expected_accessions": len(manifest),
        "verified_raw": verified,
        "controlled_retry": len(manifest) - verified,
        "manifest_sha256": hashlib.sha256(edgar_remaining.FULL.read_bytes()).hexdigest(),
        "batch_plan_sha256": plan_sha256,
        "cohort_by_ticker": dict(sorted(cohort_counts.items())),
        "http_attempts_total": sum(seen_attempts.values()),
        "evidence_databases": len(paths),
        "raw_storage": "append-only SQLite evidence.raw_response BLOB",
        "timestamp": {
            "raw_present": sum(bool(item["acceptance_datetime_raw"]) for item in metadata),
            "converted": timestamp_converted,
            "not_verifiable": timestamp_not_verifiable,
            "interpretation": "America/New_York wall clock converted with IANA DST rules",
            "limitation": "SEC ACCEPTANCE-DATETIME is not proven public dissemination time",
        },
        "checks": dict(sorted(checks.items())),
        "discrepancies": dict(Counter(item["severity"] for item in discrepancies)),
        "orphan_records": len(orphans),
        "contradictory_evidence_accessions": len(contradictory),
        "authorized_controlled_transitions": len(authorized_transitions),
        "checkpoint_consistent": checkpoint_consistent,
        "metadata_export": str(METADATA.relative_to(ROOT)),
        "discrepancy_export": str(DISCREPANCIES.relative_to(ROOT)),
    }
    metadata.sort(key=lambda item: item["accession_number"])
    discrepancies.sort(key=lambda item: (item["severity"], item["accession"], item["check_name"]))
    write_csv(METADATA, METADATA_FIELDS, metadata)
    write_csv(DISCREPANCIES, DISCREPANCY_FIELDS, discrepancies)
    temporary = SUMMARY.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(SUMMARY)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(audit(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
