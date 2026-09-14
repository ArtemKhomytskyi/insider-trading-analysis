"""Read-only validation for Phase 4 entity dimensions."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.db import PROJECT_ROOT, get_connection, table_exists

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "insider_research.db"
EXPORT_DIR = PROJECT_ROOT / "data" / "exports" / "entity_normalization"


@dataclass
class EntityValidationCheck:
    check_id: str
    check_name: str
    severity: str
    metric_value: str
    threshold: str
    description: str
    recommended_action: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def scalar(conn: sqlite3.Connection, sql: str) -> Any:
    return conn.execute(sql).fetchone()[0]


def severity_from_count(count: int, *, fail: bool = True) -> str:
    if count > 0:
        return "FAIL" if fail else "WARN"
    return "PASS"


def export_csv(df: pd.DataFrame, filename: str, export_dir: Path) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(export_dir / filename, index=False)
    logger.info("exported %s (%s rows)", filename, len(df))


def linkage_coverage(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        WITH totals AS (
            SELECT COUNT(*) AS clean_rows,
                   SUM(CASE WHEN include_mvp_alpha = 1 THEN 1 ELSE 0 END) AS mvp_rows
            FROM clean_insider_transactions
        ),
        linked AS (
            SELECT
                SUM(CASE WHEN ci.insider_cik IS NOT NULL THEN 1 ELSE 0 END) AS linked_insiders,
                SUM(CASE WHEN cd.issuer_cik IS NOT NULL THEN 1 ELSE 0 END) AS linked_companies,
                SUM(CASE WHEN ci.insider_cik IS NOT NULL AND cd.issuer_cik IS NOT NULL THEN 1 ELSE 0 END) AS linked_both,
                SUM(CASE WHEN t.include_mvp_alpha = 1 AND ci.insider_cik IS NOT NULL AND cd.issuer_cik IS NOT NULL THEN 1 ELSE 0 END) AS mvp_linked_both
            FROM clean_insider_transactions t
            LEFT JOIN clean_insiders ci ON t.insider_cik = ci.insider_cik
            LEFT JOIN company_dimension cd ON t.issuer_cik = cd.issuer_cik
        )
        SELECT 'clean_transactions_linked_to_insiders_pct' AS metric,
               ROUND(100.0 * linked_insiders / NULLIF(clean_rows, 0), 4) AS value FROM totals, linked
        UNION ALL
        SELECT 'clean_transactions_linked_to_companies_pct',
               ROUND(100.0 * linked_companies / NULLIF(clean_rows, 0), 4) FROM totals, linked
        UNION ALL
        SELECT 'clean_transactions_linked_to_both_pct',
               ROUND(100.0 * linked_both / NULLIF(clean_rows, 0), 4) FROM totals, linked
        UNION ALL
        SELECT 'mvp_transactions_linked_to_both_pct',
               ROUND(100.0 * mvp_linked_both / NULLIF(mvp_rows, 0), 4) FROM totals, linked
        """,
        conn,
    )


def run_validation(conn: sqlite3.Connection, export_dir: Path = EXPORT_DIR) -> pd.DataFrame:
    checks: list[EntityValidationCheck] = []
    metadata_exists = table_exists(conn, "entity_build_metadata")
    metadata_status = "MISSING"
    if metadata_exists:
        row = conn.execute(
            """
            SELECT status
            FROM entity_build_metadata
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        metadata_status = row[0] if row else "MISSING"
    checks.append(
        EntityValidationCheck(
            "E_META",
            "entity_build_status_complete",
            "PASS" if metadata_status == "COMPLETE" else "FAIL",
            metadata_status,
            "COMPLETE",
            "Latest entity build metadata must show a completed production build",
            "Run python -m src.entities.build_entity_dimensions --reset without --limit",
        )
    )
    required = [
        "clean_insider_transactions",
        "clean_insiders",
        "insider_name_aliases",
        "company_dimension",
        "company_identifier_history",
        "insider_company_roles",
    ]
    missing = [name for name in required if not table_exists(conn, name)]
    checks.append(
        EntityValidationCheck(
            "E00",
            "entity_tables_exist",
            "FAIL" if missing else "PASS",
            ",".join(missing) or "none",
            "none missing",
            "All Phase 4 tables must exist",
            "Run python -m src.entities.build_entity_dimensions --reset",
        )
    )
    if missing:
        summary = pd.DataFrame([c.to_dict() for c in checks])
        export_csv(summary, "entity_validation_summary.csv", export_dir)
        return summary
    if metadata_status != "COMPLETE":
        summary = pd.DataFrame([c.to_dict() for c in checks])
        export_csv(summary, "entity_validation_summary.csv", export_dir)
        return summary

    sql_checks = [
        ("I01", "clean_insider_null_cik", "SELECT COUNT(*) FROM clean_insiders WHERE insider_cik IS NULL OR TRIM(insider_cik) = ''", "Insider CIK cannot be null"),
        ("I02", "clean_insider_duplicate_cik", "SELECT COUNT(*) FROM (SELECT insider_cik FROM clean_insiders GROUP BY insider_cik HAVING COUNT(*) > 1)", "Insider CIK must be unique"),
        ("I03", "clean_insider_zero_transactions", "SELECT COUNT(*) FROM clean_insiders WHERE transaction_count <= 0 OR transaction_count IS NULL", "Insiders must have transactions"),
        ("I04", "clean_insider_bad_seen_range", "SELECT COUNT(*) FROM clean_insiders WHERE first_seen_date > last_seen_date", "Insider first_seen_date must be <= last_seen_date"),
        ("I05", "canonical_name_blank_when_source_exists", "SELECT COUNT(*) FROM clean_insiders i WHERE (canonical_name IS NULL OR TRIM(canonical_name) = '') AND EXISTS (SELECT 1 FROM insider_name_aliases a WHERE a.insider_cik = i.insider_cik AND TRIM(a.raw_name) <> '')", "Canonical name required when source names exist"),
        ("C01", "company_duplicate_cik", "SELECT COUNT(*) FROM (SELECT issuer_cik FROM company_dimension GROUP BY issuer_cik HAVING COUNT(*) > 1)", "Issuer CIK must be unique"),
        ("C02", "company_zero_transactions", "SELECT COUNT(*) FROM company_dimension WHERE transaction_count <= 0 OR transaction_count IS NULL", "Companies must have transactions"),
        ("C03", "company_bad_seen_range", "SELECT COUNT(*) FROM company_dimension WHERE first_seen_date > last_seen_date", "Company first_seen_date must be <= last_seen_date"),
        ("C04", "invalid_canonical_ticker", "SELECT COUNT(*) FROM company_dimension d WHERE current_or_latest_ticker IS NOT NULL AND NOT EXISTS (SELECT 1 FROM company_identifier_history h WHERE h.issuer_cik = d.issuer_cik AND h.ticker = d.current_or_latest_ticker AND h.ticker_is_valid = 1)", "Canonical ticker must be valid or NULL"),
        ("R01", "role_orphan_insider", "SELECT COUNT(*) FROM insider_company_roles r LEFT JOIN clean_insiders i ON r.insider_cik = i.insider_cik WHERE i.insider_cik IS NULL", "Role insider CIKs must exist"),
        ("R02", "role_orphan_company", "SELECT COUNT(*) FROM insider_company_roles r LEFT JOIN company_dimension c ON r.issuer_cik = c.issuer_cik WHERE c.issuer_cik IS NULL", "Role issuer CIKs must exist"),
        ("R03", "role_bad_seen_range", "SELECT COUNT(*) FROM insider_company_roles WHERE first_seen_date > last_seen_date", "Role first_seen_date must be <= last_seen_date"),
    ]
    for check_id, name, sql, desc in sql_checks:
        count = int(scalar(conn, sql) or 0)
        checks.append(
            EntityValidationCheck(check_id, name, severity_from_count(count), str(count), "0", desc, "Rebuild and inspect Phase 4 QA exports")
        )

    same_name_cross_cik = int(
        scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT normalized_name
                FROM insider_name_aliases
                WHERE normalized_name IS NOT NULL AND TRIM(normalized_name) <> ''
                GROUP BY normalized_name
                HAVING COUNT(DISTINCT insider_cik) > 1
            )
            """,
        )
        or 0
    )
    checks.append(
        EntityValidationCheck(
            "I06",
            "same_name_different_cik_preserved",
            "PASS",
            str(same_name_cross_cik),
            "reported only",
            "Same normalized names across CIKs are QA cases, not automatic merges",
            "Review insider_same_name_different_cik.csv",
        )
    )

    coverage = linkage_coverage(conn)
    export_csv(coverage, "entity_linkage_coverage.csv", export_dir)
    mvp_linkage = float(coverage.loc[coverage["metric"] == "mvp_transactions_linked_to_both_pct", "value"].iloc[0] or 0)
    checks.append(
        EntityValidationCheck(
            "L01",
            "mvp_entity_linkage_pct",
            "FAIL" if mvp_linkage < 99.9 else "PASS",
            f"{mvp_linkage:.4f}",
            ">= 99.9",
            "MVP transactions should link to both insider and company dimensions",
            "Inspect missing CIK rows in clean_insider_transactions",
        )
    )

    summary = pd.DataFrame([c.to_dict() for c in checks])
    export_csv(summary, "entity_validation_summary.csv", export_dir)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Phase 4 entity dimensions.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--out-dir", type=Path, default=EXPORT_DIR)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    conn = get_connection(args.db_path)
    try:
        summary = run_validation(conn, args.out_dir)
    finally:
        conn.close()
    return 1 if (summary["severity"] == "FAIL").any() else 0


if __name__ == "__main__":
    sys.exit(main())
