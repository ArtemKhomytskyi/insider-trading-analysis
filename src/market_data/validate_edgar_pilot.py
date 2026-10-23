"""Offline validation of the 20-filing EDGAR pilot; never contacts SEC."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from src.market_data.edgar_timestamp_pilot import archive_url, eastern_to_utc, readonly_db, verify_header

SEC_TIMEZONE_SOURCE = "https://www.sec.gov/files/about/webmaster-faq.htm"
SEC_DISSEMINATION_SOURCE = "https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data"


def validate(manifest: Path, pilot_db: Path) -> tuple[list[dict], dict]:
    with manifest.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 20 or len({row["accession_number"] for row in rows}) != 20:
        raise ValueError("Expected exact 20-filing manifest")
    db = readonly_db(pilot_db)
    observations: list[dict] = []
    try:
        for row in rows:
            acc = row["accession_number"]
            saved = db.execute("""SELECT verification_status,raw_response,response_sha256,raw_acceptance,
                                 parsed_utc,source_url,http_status,timezone_status FROM evidence
                                 WHERE accession_number=? ORDER BY id DESC LIMIT 1""", (acc,)).fetchone()
            if saved is None:
                observations.append(dict(accession_number=acc, ticker=row["ticker"], status="NOT_REQUESTED"))
                continue
            state, body, digest, raw, stored_utc, url, http_status, timezone_status = saved
            item = dict(accession_number=acc, ticker=row["ticker"], document_type=row["document_type"],
                        filing_date=row["filing_date"], status=state, http_status=http_status,
                        raw_acceptance=raw, stored_utc=stored_utc, timezone_status=timezone_status,
                        source_url=url, sha256=digest, errors=[])
            if state == "VERIFIED_RAW":
                if body is None or hashlib.sha256(body).hexdigest() != digest:
                    item["errors"].append("response_hash_mismatch")
                if url != archive_url(row):
                    item["errors"].append("source_url_mismatch")
                try:
                    parsed_raw, _ = verify_header(body or b"", row)
                    if parsed_raw != raw:
                        item["errors"].append("raw_acceptance_mismatch")
                except ValueError as exc:
                    item["errors"].append("header_identity:" + str(exc))
                if stored_utc is not None or timezone_status != "UNVERIFIED":
                    item["errors"].append("unverified_timezone_was_promoted")
                try:
                    wall = datetime.strptime(raw, "%Y%m%d%H%M%S")
                    item["acceptance_date"] = wall.date().isoformat()
                    item["acceptance_hour"] = wall.hour
                    item["acceptance_weekday"] = wall.strftime("%A")
                    item["inferred_new_york_utc"] = eastern_to_utc(raw)
                    item["utc_interpretation"] = "INFERRED_NOT_STORED"
                except (TypeError, ValueError) as exc:
                    item["errors"].append("timestamp_parse:" + str(exc))
            observations.append(item)
        unknown = db.execute("SELECT DISTINCT accession_number FROM evidence").fetchall()
        if {r[0] for r in unknown} - {r["accession_number"] for r in rows}:
            raise ValueError("Pilot DB contains accession outside manifest")
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        attempts = db.execute("SELECT count(*) FROM http_attempts").fetchone()[0]
    finally:
        db.close()
    complete = [r for r in observations if r["status"] == "VERIFIED_RAW"]
    summary = {
        "manifest_count": len(rows), "verified_raw": len(complete),
        "status_counts": dict(Counter(r["status"] for r in observations)),
        "evidence_errors": sum(len(r.get("errors", [])) for r in observations),
        "conflicting_accessions": [r["accession_number"] for r in observations if r.get("errors")],
        "raw_acceptance_min": min((r["raw_acceptance"] for r in complete), default=None),
        "raw_acceptance_max": max((r["raw_acceptance"] for r in complete), default=None),
        "filing_year_distribution": dict(sorted(Counter(r["filing_date"][:4] for r in complete).items())),
        "ticker_distribution": dict(Counter(r["ticker"] for r in complete)),
        "raw_hour_distribution": dict(sorted(Counter(str(r.get("acceptance_hour")) for r in complete).items())),
        "weekday_distribution": dict(Counter(r.get("acceptance_weekday") for r in complete)),
        "after_1600_et_inferred": sum(r.get("acceptance_hour", -1) >= 16 for r in complete),
        "acceptance_date_vs_filing_date": dict(Counter(
            "same" if r.get("acceptance_date") == r["filing_date"] else "different" for r in complete)),
        "inferred_new_york_utc_offset_hours": dict(Counter(str(int((
            datetime.fromisoformat(r["inferred_new_york_utc"]).replace(tzinfo=None) -
            datetime.strptime(r["raw_acceptance"], "%Y%m%d%H%M%S")).total_seconds() / 3600))
            for r in complete if r.get("inferred_new_york_utc"))),
        "stored_utc_count": sum(r.get("stored_utc") is not None for r in complete),
        "sqlite_integrity": integrity, "attempts_total": attempts,
        "timezone_interpretation": "SEC FAQ labels Acceptance Time EST; America/New_York DST conversion is inferred, not stored or proof of dissemination",
        "timezone_sources": [SEC_TIMEZONE_SOURCE, SEC_DISSEMINATION_SOURCE],
    }
    return observations, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--pilot-db", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    rows, summary = validate(args.manifest, args.pilot_db)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"summary": summary, "filings": rows}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if summary["evidence_errors"] or summary["sqlite_integrity"] != "ok":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
