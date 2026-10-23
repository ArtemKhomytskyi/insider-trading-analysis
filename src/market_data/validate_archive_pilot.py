"""Read-only archive-pilot validation and coverage exports."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

EXPECTED = {"NFLX": 2984, "CRM": 2986, "TSLA": 198}
QA = {"P5.1-QA-001": 10, "P5.1-QA-002": 14, "P5.1-QA-003": 57}
ARCHIVE_SHA = "d9317c8fb2d63b9b00db5f933b6c9639d2bf7ea3b918169bb5cec5903dce85a1"


def validate(db_path: Path, out_dir: Path) -> dict:
    if not db_path.is_file():
        raise ValueError(f"Missing pilot database: {db_path}")
    db = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        securities = [dict(r) for r in db.execute("SELECT * FROM securities ORDER BY ticker")]
        coverage = [dict(r) for r in db.execute(
            "SELECT s.ticker,s.issuer_cik,s.valid_from,s.valid_to,c.bar_count,c.first_date,c.last_date,"
            "c.finding_count FROM security_coverage c JOIN securities s USING(security_id) ORDER BY s.ticker")]
        years = [dict(r) for r in db.execute(
            "SELECT s.ticker,y.year,y.bar_count,y.first_date,y.last_date FROM year_coverage y "
            "JOIN securities s USING(security_id) ORDER BY s.ticker,y.year")]
        cohorts = [dict(r) for r in db.execute(
            "SELECT s.ticker,c.year,c.transaction_code,c.total,c.qa_excluded,c.price_window_covered "
            "FROM cohort c JOIN securities s USING(security_id) ORDER BY s.ticker,c.year,c.transaction_code")]
        findings = [dict(r) for r in db.execute(
            "SELECT COALESCE(s.ticker,'UNMATCHED') ticker,f.trading_date,f.code,f.detail "
            "FROM findings f LEFT JOIN securities s USING(security_id) ORDER BY ticker,f.trading_date,f.code")]
        qa = {r["qa_id"]: r["n"] for r in db.execute(
            "SELECT qa_id,COUNT(*) n FROM qa_quarantine GROUP BY qa_id")}
        provenance = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM provenance")}
        count = lambda sql: db.execute(sql).fetchone()[0]
        checks = {
            "sqlite_integrity": count("PRAGMA integrity_check") == "ok",
            "exact_three_approved": {r["ticker"] for r in securities} == set(EXPECTED)
                and len(securities) == 3 and all(r["identity_status"] == "APPROVED"
                and r["identity_evidence"].strip() for r in securities),
            "expected_bar_counts": {r["ticker"]: r["bar_count"] for r in coverage} == EXPECTED,
            "exact_81_quarantine": qa == QA,
            "no_other_security_bars": count(
                "SELECT COUNT(*) FROM bars b LEFT JOIN securities s USING(security_id) "
                "WHERE s.security_id IS NULL") == 0,
            "within_mapping_and_cutoff": count(
                "SELECT COUNT(*) FROM bars b JOIN securities s USING(security_id) "
                "WHERE b.trading_date<s.valid_from OR b.trading_date>s.valid_to "
                "OR b.trading_date>'2017-11-10'") == 0,
            "canonical_stock_files_only": count(
                "SELECT COUNT(*) FROM bars WHERE source_file NOT LIKE 'Data/Stocks/%.us.txt'") == 0,
            "adjustment_semantics_unknown": count(
                "SELECT COUNT(*) FROM bars WHERE adjustment_semantics<>'UNKNOWN'") == 0,
            "source_text_preserved": count(
                "SELECT COUNT(*) FROM bars WHERE source_date_text<>trading_date OR "
                "CAST(open_source_text AS REAL)<>open OR CAST(high_source_text AS REAL)<>high OR "
                "CAST(low_source_text AS REAL)<>low OR CAST(close_source_text AS REAL)<>close OR "
                "CAST(volume_source_text AS REAL)<>volume") == 0,
            "no_invalid_or_duplicate_bars": count(
                "SELECT COUNT(*) FROM findings WHERE code IN ('INVALID_BAR','DUPLICATE_BAR')") == 0,
            "qa_excluded_from_cohorts": count(
                "SELECT COALESCE(SUM(qa_excluded),0) FROM cohort") == 0,
            "cohort_years_within_approved_intervals": count(
                "SELECT COUNT(*) FROM cohort c JOIN securities s USING(security_id) "
                "WHERE c.year<CAST(substr(s.valid_from,1,4) AS INTEGER) "
                "OR c.year>CAST(substr(s.valid_to,1,4) AS INTEGER)") == 0,
            "all_source_hashes_recorded": count("SELECT COUNT(*) FROM source_files") == 17078
                and count("SELECT COUNT(*) FROM source_files WHERE length(sha256)<>64") == 0,
            "archive_hash": provenance.get("archive_sha256") == ARCHIVE_SHA,
            "limited_owner_acceptance": "owner_acceptance" in provenance
                and "unverified" in str(provenance.get("provenance_limitations", "")).lower(),
        }
        summary = {
            "status": "PASS" if all(checks.values()) else "FAIL",
            "checks": checks,
            "quarantine": qa,
            "coverage": coverage,
            "findings_by_code": {r["code"]: r["n"] for r in db.execute(
                "SELECT code,COUNT(*) n FROM findings GROUP BY code")},
            "cohort_totals": {t: {
                "transactions": sum(c["total"] for c in cohorts if c["ticker"] == t),
                "date_matched": sum(c["price_window_covered"] for c in cohorts if c["ticker"] == t),
            } for t in EXPECTED},
            "provenance": provenance,
        }
    finally:
        db.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("security_coverage.csv", coverage), ("year_coverage.csv", years),
                       ("sec_cohort_coverage.csv", cohorts), ("findings.csv", findings)):
        with (out_dir / name).open("w", newline="", encoding="utf-8") as handle:
            if rows:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-db", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = validate(args.pilot_db, args.out_dir)
    print(json.dumps({k: v for k, v in summary.items() if k != "provenance"}, indent=2))
    if summary["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
