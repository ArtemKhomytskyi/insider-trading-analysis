"""Build an offline, exact-mapping accession manifest for EDGAR recovery."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from src.market_data.edgar_timestamp_pilot import APPROVED, END, readonly_db

FIELDS = ["accession_number", "ticker", "historical_security_id", "mapping_id", "issuer_cik",
          "approved_valid_from", "approved_valid_to", "first_transaction_date", "last_transaction_date",
          "document_type", "filing_date", "reporting_owner_ciks", "local_transaction_count",
          "source_folder", "archive_cik_candidate", "archive_cik_basis", "eligibility_status", "eligibility_issue"]


def matched_tsv_records(path: Path, accessions: set[str]) -> list[dict[str, str]]:
    """Use ripgrep's fixed-string scanner; verify the first field after matching."""
    with path.open(encoding="utf-8-sig") as handle:
        header = handle.readline().rstrip("\r\n")
    result = subprocess.run(["rg", "-F", "-f", "-", "--color", "never", "--", str(path)],
                            input="\n".join(sorted(accessions)) + "\n", capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False)
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"rg failed for {path}: {result.stderr[:200]}")
    records = csv.DictReader(io.StringIO(header + "\n" + result.stdout), delimiter="\t")
    return [record for record in records if record.get("ACCESSION_NUMBER") in accessions]


def build_manifest(production_db: Path, raw_sec_dir: Path) -> list[dict[str, str]]:
    grouped: dict[str, dict] = {}
    db = readonly_db(production_db)
    try:
        for ticker, (issuer, mapping, start) in APPROVED.items():
            query = """SELECT t.accession_number,t.insider_cik,t.document_type,t.filing_date,
                              t.source_folder,tm.transaction_date
                       FROM transaction_market_mapping tm
                       JOIN clean_insider_transactions t ON t.transaction_id=tm.transaction_id
                       WHERE tm.issuer_cik=? AND tm.mapping_id=? AND tm.transaction_date BETWEEN ? AND ?
                         AND t.issuer_cik=? AND t.accession_number IS NOT NULL"""
            for acc, insider, form, filing, folder, txdate in db.execute(query, (issuer, mapping, start, END, issuer)):
                row = grouped.setdefault(acc, dict(accession_number=acc, ticker=ticker,
                    historical_security_id=mapping, mapping_id=mapping, issuer_cik=issuer,
                    approved_valid_from=start, approved_valid_to=END, transaction_dates=[],
                    document_types=set(), filing_dates=set(), source_folders=set(), clean_owners=set()))
                if row["issuer_cik"] != issuer or row["mapping_id"] != mapping:
                    raise ValueError(f"Accession spans different historical securities: {acc}")
                row["transaction_dates"].append(txdate)
                row["document_types"].add(form or "")
                row["filing_dates"].add(filing or "")
                row["source_folders"].add(folder or "")
                row["clean_owners"].add(str(insider or "").zfill(10))
        if len(grouped) != 6067:
            raise ValueError(f"Expected 6,067 exact-mapping accessions; found {len(grouped)}")
        by_folder: dict[str, set[str]] = defaultdict(set)
        for acc, row in grouped.items():
            for folder in row["source_folders"]:
                by_folder[folder].add(acc)
        for folder, wanted in by_folder.items():
            path = raw_sec_dir / folder / "REPORTINGOWNER.tsv"
            if not path.is_file():
                raise FileNotFoundError(path)
            for raw in matched_tsv_records(path, wanted):
                acc = raw["ACCESSION_NUMBER"]
                grouped[acc].setdefault("raw_owners", set()).add(str(raw.get("RPTOWNERCIK", "")).strip().zfill(10))
            submission_path = raw_sec_dir / folder / "SUBMISSION.tsv"
            if not submission_path.is_file():
                raise FileNotFoundError(submission_path)
            for raw in matched_tsv_records(submission_path, wanted):
                acc = raw["ACCESSION_NUMBER"]
                grouped[acc].setdefault("raw_submissions", set()).add((raw.get("DOCUMENT_TYPE", ""),
                    raw.get("FILING_DATE", ""), raw.get("ISSUERCIK", "")))
        result = []
        for acc, row in grouped.items():
            issues = []
            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", acc or ""):
                issues.append("invalid_accession")
            if len(row["document_types"]) != 1 or next(iter(row["document_types"])) not in {"4", "4/A", "5", "5/A"}:
                issues.append("document_type_conflict_or_unsupported")
            if len(row["filing_dates"]) != 1 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", next(iter(row["filing_dates"]))):
                issues.append("filing_date_conflict_or_missing")
            if len(row["source_folders"]) != 1:
                issues.append("source_folder_conflict")
            owners = sorted(row.get("raw_owners", set()))
            if not owners or any(not re.fullmatch(r"\d{10}", x) for x in owners):
                issues.append("reporting_owner_missing_or_invalid")
            if not row["clean_owners"].issubset(set(owners)):
                issues.append("clean_owner_not_in_raw_filing")
            if not all(row["approved_valid_from"] <= d <= row["approved_valid_to"] for d in row["transaction_dates"]):
                issues.append("transaction_outside_approved_interval")
            submissions = row.get("raw_submissions", set())
            if len(submissions) != 1:
                issues.append("raw_submission_missing_or_conflicting")
            else:
                form, raw_date, raw_issuer = next(iter(submissions))
                try:
                    normalized_date = datetime.strptime(raw_date, "%d-%b-%Y").date().isoformat()
                except ValueError:
                    normalized_date = raw_date
                if (form != next(iter(row["document_types"])) or
                        normalized_date != next(iter(row["filing_dates"])) or raw_issuer != row["issuer_cik"]):
                    issues.append("raw_submission_metadata_mismatch")
            prefix = acc[:10]
            candidate = prefix if prefix in owners else (owners[0] if owners else "")
            result.append({
                "accession_number": acc, "ticker": row["ticker"],
                "historical_security_id": row["historical_security_id"], "mapping_id": row["mapping_id"],
                "issuer_cik": row["issuer_cik"], "approved_valid_from": row["approved_valid_from"],
                "approved_valid_to": row["approved_valid_to"],
                "first_transaction_date": min(row["transaction_dates"]),
                "last_transaction_date": max(row["transaction_dates"]),
                "document_type": next(iter(row["document_types"])) if len(row["document_types"]) == 1 else "",
                "filing_date": next(iter(row["filing_dates"])) if len(row["filing_dates"]) == 1 else "",
                "reporting_owner_ciks": "|".join(owners), "local_transaction_count": str(len(row["transaction_dates"])),
                "source_folder": next(iter(row["source_folders"])) if len(row["source_folders"]) == 1 else "",
                "archive_cik_candidate": str(int(candidate)) if candidate.isdigit() else "",
                "archive_cik_basis": "accession_prefix_is_reported_owner_candidate_unverified" if prefix in owners else "first_reported_owner_candidate_unverified",
                "eligibility_status": "READY" if not issues else "NEEDS_REVIEW",
                "eligibility_issue": "|".join(issues),
            })
        return sorted(result, key=lambda r: (r["ticker"], r["filing_date"], r["accession_number"]))
    finally:
        db.close()


def write_manifest(rows: list[dict[str, str]], output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-db", type=Path, required=True)
    parser.add_argument("--raw-sec-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = build_manifest(args.production_db, args.raw_sec_dir)
    digest = write_manifest(rows, args.output)
    print(json.dumps({"rows": len(rows), "status": dict(Counter(r["eligibility_status"] for r in rows)),
                      "issues": dict(Counter(issue for r in rows for issue in r["eligibility_issue"].split("|") if issue)),
                      "ticker": dict(Counter(r["ticker"] for r in rows)),
                      "transactions": sum(int(r["local_transaction_count"]) for r in rows),
                      "sha256": digest}, indent=2))


if __name__ == "__main__":
    main()
