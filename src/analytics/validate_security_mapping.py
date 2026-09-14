"""Read-only validation for Phase 5.1 historical security mapping."""

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
EXPORT_DIR = PROJECT_ROOT / "data" / "exports" / "security_mapping"


@dataclass
class MappingValidationCheck:
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


def coverage(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT universe,
               COUNT(*) AS total_transactions,
               SUM(is_mapped) AS mapped_transactions,
               SUM(CASE WHEN is_mapped = 0 THEN 1 ELSE 0 END) AS unmapped_transactions,
               SUM(is_ambiguous) AS ambiguous_transactions,
               ROUND(100.0 * SUM(is_mapped) / NULLIF(COUNT(*), 0), 4) AS mapped_pct
        FROM (
            SELECT 'all_clean' AS universe, tm.*
            FROM transaction_market_mapping tm
            UNION ALL
            SELECT 'mvp', tm.*
            FROM transaction_market_mapping tm
            JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
            WHERE t.include_mvp_alpha = 1
            UNION ALL
            SELECT 'P', tm.*
            FROM transaction_market_mapping tm
            JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
            WHERE t.transaction_code = 'P'
            UNION ALL
            SELECT 'S', tm.*
            FROM transaction_market_mapping tm
            JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
            WHERE t.transaction_code = 'S'
        )
        GROUP BY universe
        """,
        conn,
    )


def run_validation(conn: sqlite3.Connection, export_dir: Path = EXPORT_DIR) -> pd.DataFrame:
    checks: list[MappingValidationCheck] = []
    required = [
        "security_identifier_mapping",
        "transaction_market_mapping",
        "company_dimension",
        "clean_insider_transactions",
    ]
    missing = [name for name in required if not table_exists(conn, name)]
    metadata_status = "MISSING"
    if table_exists(conn, "security_mapping_build_metadata"):
        row = conn.execute(
            "SELECT status FROM security_mapping_build_metadata ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        metadata_status = row[0] if row else "MISSING"
    checks.append(
        MappingValidationCheck(
            "M_META",
            "security_mapping_build_complete",
            "PASS" if metadata_status == "COMPLETE" else "FAIL",
            metadata_status,
            "COMPLETE",
            "Latest Phase 5.1 mapping build must be a complete production build",
            "Run python -m src.mapping.build_security_mapping --reset without --limit",
        )
    )
    checks.append(
        MappingValidationCheck(
            "M00",
            "mapping_tables_exist",
            "FAIL" if missing else "PASS",
            ",".join(missing) or "none",
            "none missing",
            "Phase 5.1 mapping tables must exist",
            "Run python -m src.mapping.build_security_mapping --reset",
        )
    )
    if missing:
        summary = pd.DataFrame([c.to_dict() for c in checks])
        export_csv(summary, "mapping_validation_summary.csv", export_dir)
        return summary
    if metadata_status != "COMPLETE":
        summary = pd.DataFrame([c.to_dict() for c in checks])
        export_csv(summary, "mapping_validation_summary.csv", export_dir)
        return summary

    sql_checks = [
        ("M01", "mapping_id_unique", "SELECT COUNT(*) FROM (SELECT mapping_id FROM security_identifier_mapping GROUP BY mapping_id HAVING COUNT(*) > 1)", "Mapping IDs must be unique"),
        ("M01B", "mapping_business_key_unique", "SELECT COUNT(*) FROM (SELECT issuer_cik, ticker_clean, COALESCE(security_title, ''), valid_from, valid_to, mapping_source, mapping_method FROM security_identifier_mapping GROUP BY issuer_cik, ticker_clean, COALESCE(security_title, ''), valid_from, valid_to, mapping_source, mapping_method HAVING COUNT(*) > 1)", "Mapping business key must be unique"),
        ("M02", "transaction_id_unique", "SELECT COUNT(*) FROM (SELECT transaction_id FROM transaction_market_mapping GROUP BY transaction_id HAVING COUNT(*) > 1)", "Each transaction can have at most one mapping row"),
        ("M03", "orphan_mapping_issuer", "SELECT COUNT(*) FROM security_identifier_mapping m LEFT JOIN company_dimension c ON m.issuer_cik = c.issuer_cik WHERE c.issuer_cik IS NULL", "Mapped issuer CIK must exist in company_dimension"),
        ("M04", "orphan_transaction_mapping_issuer", "SELECT COUNT(*) FROM transaction_market_mapping tm LEFT JOIN company_dimension c ON tm.issuer_cik = c.issuer_cik WHERE tm.issuer_cik IS NOT NULL AND c.issuer_cik IS NULL", "Transaction mapped issuer must exist in company_dimension"),
        ("M05", "invalid_ticker_accepted", "SELECT COUNT(*) FROM security_identifier_mapping WHERE market_data_symbol IS NOT NULL AND (ticker_clean IS NULL OR TRIM(ticker_clean) = '' OR mapping_confidence = 'LOW')", "Invalid placeholders cannot become accepted market symbols"),
        ("M06", "bad_validity_interval", "SELECT COUNT(*) FROM security_identifier_mapping WHERE valid_from IS NOT NULL AND valid_to IS NOT NULL AND valid_from > valid_to", "Mapping valid_from must be <= valid_to"),
        ("M07", "mapped_ambiguous_transactions", "SELECT COUNT(*) FROM transaction_market_mapping WHERE is_mapped = 1 AND is_ambiguous = 1", "Ambiguous mappings cannot be accepted"),
        ("M08", "mapped_without_symbol", "SELECT COUNT(*) FROM transaction_market_mapping WHERE is_mapped = 1 AND (market_data_symbol IS NULL OR mapping_id IS NULL)", "Accepted mappings require symbol and mapping_id"),
        ("M12", "mapped_without_resolution_method", "SELECT COUNT(*) FROM transaction_market_mapping WHERE is_mapped = 1 AND (resolution_method IS NULL OR TRIM(resolution_method) = '')", "Mapped transactions require deterministic resolution_method"),
        ("M13", "candidate_counts_inconsistent", "SELECT COUNT(*) FROM transaction_market_mapping WHERE candidate_count_raw < candidate_count_logical OR candidate_count_raw IS NULL OR candidate_count_logical IS NULL", "Candidate count semantics must be internally consistent"),
        ("M14", "multi_candidate_marked_mapped_without_method", "SELECT COUNT(*) FROM transaction_market_mapping WHERE is_mapped = 1 AND candidate_count_logical > 1 AND resolution_method NOT IN ('CIK_DATE_TICKER_EXACT')", "Multi-candidate mapped rows require exact ticker resolution"),
        ("M15", "cross_cik_transaction_mapping", "SELECT COUNT(*) FROM transaction_market_mapping tm JOIN security_identifier_mapping m ON tm.mapping_id = m.mapping_id WHERE tm.is_mapped = 1 AND tm.issuer_cik <> m.issuer_cik", "Mapped transactions cannot cross issuer CIKs"),
    ]
    for check_id, name, sql, desc in sql_checks:
        count = int(scalar(conn, sql) or 0)
        checks.append(
            MappingValidationCheck(check_id, name, severity_from_count(count), str(count), "0", desc, "Inspect security mapping exports and rebuild")
        )

    overlap_count = int(
        scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM security_identifier_mapping a
            JOIN security_identifier_mapping b
              ON a.mapping_id < b.mapping_id
             AND a.issuer_cik = b.issuer_cik
             AND a.ticker_clean = b.ticker_clean
             AND COALESCE(a.security_title, '') = COALESCE(b.security_title, '')
             AND a.valid_from <= b.valid_to
             AND b.valid_from <= a.valid_to
            """,
        )
        or 0
    )
    checks.append(
        MappingValidationCheck(
            "M08B",
            "same_issuer_ticker_overlapping_intervals",
            severity_from_count(overlap_count, fail=False),
            str(overlap_count),
            "reported/review",
            "Overlapping observed intervals are flagged; transaction mapping treats multi-candidate dates as ambiguous",
            "Review mapping candidates before market-data acceptance",
        )
    )

    ticker_reuse = int(
        scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT ticker_clean FROM security_identifier_mapping
                WHERE ticker_clean IS NOT NULL
                GROUP BY ticker_clean
                HAVING COUNT(DISTINCT issuer_cik) > 1
            )
            """,
        )
        or 0
    )
    ticker_changes = int(
        scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT issuer_cik FROM security_identifier_mapping
                GROUP BY issuer_cik
                HAVING COUNT(DISTINCT ticker_clean) > 1
            )
            """,
        )
        or 0
    )
    checks.extend(
        [
            MappingValidationCheck(
                "M09",
                "ticker_reuse_preserved",
                "PASS",
                str(ticker_reuse),
                "reported only",
                "Ticker reuse across CIKs is preserved and reported, not merged",
                "Review tickers_multiple_ciks.csv",
            ),
            MappingValidationCheck(
                "M10",
                "historical_ticker_changes_preserved",
                "PASS",
                str(ticker_changes),
                "reported only",
                "Issuer CIK ticker changes are preserved historically",
                "Review ticker_change_candidates.csv",
            ),
        ]
    )
    cov = coverage(conn)
    export_csv(cov, "transaction_mapping_coverage.csv", export_dir)
    export_csv(
        pd.read_sql_query(
            """
            SELECT COALESCE(ambiguity_reason, 'NONE') AS ambiguity_reason,
                   COUNT(*) AS transaction_count,
                   SUM(CASE WHEN t.include_mvp_alpha = 1 THEN 1 ELSE 0 END) AS mvp_transaction_count
            FROM transaction_market_mapping tm
            LEFT JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
            GROUP BY COALESCE(ambiguity_reason, 'NONE')
            ORDER BY transaction_count DESC
            """,
            conn,
        ),
        "mapping_ambiguity_reason_distribution.csv",
        export_dir,
    )
    export_csv(
        pd.read_sql_query(
            """
            SELECT SUBSTR(tm.transaction_date, 1, 4) AS year,
                   COUNT(*) AS total_transactions,
                   SUM(tm.is_mapped) AS mapped_transactions,
                   SUM(tm.is_ambiguous) AS ambiguous_transactions,
                   ROUND(100.0 * SUM(tm.is_mapped) / NULLIF(COUNT(*), 0), 4) AS mapped_pct
            FROM transaction_market_mapping tm
            GROUP BY year
            ORDER BY year
            """,
            conn,
        ),
        "mapping_coverage_by_year.csv",
        export_dir,
    )
    export_csv(
        pd.read_sql_query(
            """
            SELECT t.transaction_code,
                   COUNT(*) AS total_transactions,
                   SUM(tm.is_mapped) AS mapped_transactions,
                   SUM(tm.is_ambiguous) AS ambiguous_transactions,
                   ROUND(100.0 * SUM(tm.is_mapped) / NULLIF(COUNT(*), 0), 4) AS mapped_pct
            FROM transaction_market_mapping tm
            JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
            GROUP BY t.transaction_code
            ORDER BY total_transactions DESC
            """,
            conn,
        ),
        "mapping_coverage_by_transaction_code.csv",
        export_dir,
    )
    export_csv(
        pd.read_sql_query(
            """
            SELECT COALESCE(resolution_method, 'NONE') AS resolution_method,
                   COUNT(*) AS transaction_count,
                   SUM(CASE WHEN t.include_mvp_alpha = 1 THEN 1 ELSE 0 END) AS mvp_transaction_count
            FROM transaction_market_mapping tm
            LEFT JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
            GROUP BY COALESCE(resolution_method, 'NONE')
            ORDER BY transaction_count DESC
            """,
            conn,
        ),
        "mapping_resolution_method_distribution.csv",
        export_dir,
    )
    mvp_rows = cov.loc[cov["universe"] == "mvp"]
    mvp_pct = float(mvp_rows["mapped_pct"].iloc[0]) if not mvp_rows.empty else 0.0
    checks.append(
        MappingValidationCheck(
            "M11",
            "mvp_mapping_coverage_reported",
            "PASS",
            f"{mvp_pct:.4f}",
            "reported",
            "MVP mapping coverage is measured without fuzzy guesses",
            "Review unmapped and ambiguous MVP exports",
        )
    )
    summary = pd.DataFrame([c.to_dict() for c in checks])
    export_csv(summary, "mapping_validation_summary.csv", export_dir)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Phase 5.1 security mapping.")
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
