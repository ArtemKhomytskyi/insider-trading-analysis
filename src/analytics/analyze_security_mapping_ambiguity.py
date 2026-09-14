"""Read-only root-cause analysis for Phase 5.1 mapping ambiguity."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

from src.utils.db import PROJECT_ROOT, get_connection

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "insider_research.db"
EXPORT_DIR = PROJECT_ROOT / "data" / "exports" / "security_mapping" / "ambiguity_analysis"


def export_query(conn: sqlite3.Connection, sql: str, filename: str, params: tuple = ()) -> pd.DataFrame:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_sql_query(sql, conn, params=params)
    df.to_csv(EXPORT_DIR / filename, index=False)
    logger.info("exported %s (%s rows)", filename, len(df))
    return df


def create_candidate_tables(conn: sqlite3.Connection, limit: int | None = None) -> None:
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    order_sql = "ORDER BY tm.transaction_id" if limit else ""
    conn.executescript(
        f"""
        DROP TABLE IF EXISTS temp.tmp_ambiguous_scope;
        CREATE TEMP TABLE tmp_ambiguous_scope AS
        SELECT tm.transaction_id, tm.issuer_cik, tm.transaction_date, tm.filing_date,
               t.ticker_clean AS transaction_ticker_clean, t.transaction_code,
               t.include_mvp_alpha, t.transaction_value, t.insider_cik
        FROM transaction_market_mapping tm
        JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
        WHERE tm.is_ambiguous = 1
        {order_sql}
        {limit_sql};
        CREATE INDEX idx_tmp_amb_scope_tx ON tmp_ambiguous_scope(transaction_id);
        CREATE INDEX idx_tmp_amb_scope_lookup ON tmp_ambiguous_scope(issuer_cik, transaction_date);

        DROP TABLE IF EXISTS temp.tmp_ambiguous_candidates;
        CREATE TEMP TABLE tmp_ambiguous_candidates AS
        SELECT s.transaction_id, s.issuer_cik, s.transaction_date, s.transaction_ticker_clean,
               s.transaction_code, s.include_mvp_alpha, s.transaction_value, s.insider_cik,
               m.mapping_id, m.ticker_clean AS candidate_ticker, m.market_data_symbol,
               m.security_title, m.valid_from, m.valid_to, m.is_ticker_change,
               m.is_reused_ticker, m.is_ambiguous AS mapping_is_ambiguous,
               m.requires_review
        FROM tmp_ambiguous_scope s
        JOIN security_identifier_mapping m
          ON s.issuer_cik = m.issuer_cik
         AND s.transaction_date >= m.valid_from
         AND s.transaction_date <= m.valid_to
        WHERE m.market_data_symbol IS NOT NULL;
        CREATE INDEX idx_tmp_amb_cand_tx ON tmp_ambiguous_candidates(transaction_id);

        DROP TABLE IF EXISTS temp.tmp_mapping_totals;
        CREATE TEMP TABLE tmp_mapping_totals AS
        SELECT COUNT(*) AS transactions,
               SUM(CASE WHEN include_mvp_alpha = 1 THEN 1 ELSE 0 END) AS mvp_transactions
        FROM clean_insider_transactions;

        DROP TABLE IF EXISTS temp.tmp_ambiguous_summary;
        CREATE TEMP TABLE tmp_ambiguous_summary AS
        SELECT transaction_id, issuer_cik, transaction_date, transaction_ticker_clean,
               transaction_code, include_mvp_alpha, transaction_value, insider_cik,
               COUNT(*) AS raw_candidate_count,
               COUNT(DISTINCT candidate_ticker || '|' || COALESCE(market_data_symbol, '') || '|' || COALESCE(security_title, '')) AS logical_candidate_count,
               COUNT(DISTINCT candidate_ticker) AS candidate_ticker_count,
               COUNT(DISTINCT market_data_symbol) AS market_symbol_count,
               SUM(CASE WHEN candidate_ticker = transaction_ticker_clean THEN 1 ELSE 0 END) AS exact_ticker_raw_count,
               COUNT(DISTINCT CASE WHEN candidate_ticker = transaction_ticker_clean THEN candidate_ticker || '|' || COALESCE(market_data_symbol, '') || '|' || COALESCE(security_title, '') END) AS exact_ticker_logical_count,
               MAX(is_ticker_change) AS involves_ticker_change,
               MAX(is_reused_ticker) AS involves_ticker_reuse,
               MAX(mapping_is_ambiguous) AS involves_mapping_ambiguity,
               MAX(requires_review) AS involves_review
        FROM tmp_ambiguous_candidates
        GROUP BY transaction_id;
        CREATE INDEX idx_tmp_amb_summary_tx ON tmp_ambiguous_summary(transaction_id);
        """
    )
    conn.commit()


def analyze(conn: sqlite3.Connection, limit: int | None = None) -> None:
    start = time.perf_counter()
    create_candidate_tables(conn, limit)
    export_query(
        conn,
        """
        SELECT reason_code,
               COUNT(*) AS ambiguous_transactions,
               SUM(CASE WHEN include_mvp_alpha = 1 THEN 1 ELSE 0 END) AS ambiguous_mvp_transactions
        FROM (
            SELECT *,
                   CASE
                     WHEN involves_mapping_ambiguity = 1 THEN 'MAPPING_REQUIRES_REVIEW'
                     WHEN logical_candidate_count = 1 AND raw_candidate_count > 1 THEN 'DUPLICATE_EQUIVALENT_CANDIDATES'
                     WHEN exact_ticker_logical_count = 1 THEN 'EXACT_TRANSACTION_TICKER_RESOLVABLE'
                     WHEN exact_ticker_logical_count > 1 THEN 'MULTIPLE_EXACT_TICKER_CANDIDATES'
                     WHEN exact_ticker_logical_count = 0 AND logical_candidate_count > 1 THEN 'NO_EXACT_TICKER_MATCH'
                     ELSE 'OTHER'
                   END AS reason_code
            FROM tmp_ambiguous_summary
        )
        GROUP BY reason_code
        ORDER BY ambiguous_mvp_transactions DESC, ambiguous_transactions DESC
        """,
        "ambiguity_root_causes.csv",
    )
    export_query(
        conn,
        """
        SELECT raw_candidate_count, logical_candidate_count, COUNT(*) AS transactions,
               SUM(CASE WHEN include_mvp_alpha = 1 THEN 1 ELSE 0 END) AS mvp_transactions
        FROM tmp_ambiguous_summary
        GROUP BY raw_candidate_count, logical_candidate_count
        ORDER BY raw_candidate_count, logical_candidate_count
        """,
        "ambiguity_candidate_count_distribution.csv",
    )
    export_query(conn, "SELECT issuer_cik, COUNT(*) AS ambiguous_transactions, SUM(include_mvp_alpha) AS ambiguous_mvp_transactions FROM tmp_ambiguous_summary GROUP BY issuer_cik ORDER BY ambiguous_mvp_transactions DESC LIMIT 500", "ambiguity_by_issuer.csv")
    export_query(conn, "SELECT transaction_ticker_clean AS ticker, COUNT(*) AS ambiguous_transactions, SUM(include_mvp_alpha) AS ambiguous_mvp_transactions FROM tmp_ambiguous_summary GROUP BY transaction_ticker_clean ORDER BY ambiguous_mvp_transactions DESC LIMIT 500", "ambiguity_by_ticker.csv")
    export_query(conn, "SELECT SUBSTR(transaction_date, 1, 4) AS year, COUNT(*) AS ambiguous_transactions, SUM(include_mvp_alpha) AS ambiguous_mvp_transactions FROM tmp_ambiguous_summary GROUP BY year ORDER BY year", "ambiguity_by_year.csv")
    export_query(conn, "SELECT transaction_code, COUNT(*) AS ambiguous_transactions, SUM(include_mvp_alpha) AS ambiguous_mvp_transactions FROM tmp_ambiguous_summary GROUP BY transaction_code ORDER BY ambiguous_transactions DESC", "ambiguity_by_transaction_code.csv")
    export_query(conn, "SELECT * FROM tmp_ambiguous_summary WHERE candidate_ticker_count = 1 LIMIT 5000", "ambiguity_same_symbol_candidates.csv")
    export_query(conn, "SELECT * FROM tmp_ambiguous_summary WHERE candidate_ticker_count > 1 LIMIT 5000", "ambiguity_multiple_symbol_candidates.csv")
    export_query(
        conn,
        """
        SELECT a.issuer_cik, a.candidate_ticker, a.mapping_id AS mapping_id_a, b.mapping_id AS mapping_id_b,
               a.valid_from AS a_from, a.valid_to AS a_to, b.valid_from AS b_from, b.valid_to AS b_to,
               COUNT(DISTINCT a.transaction_id) AS affected_transactions,
               SUM(CASE WHEN a.include_mvp_alpha = 1 THEN 1 ELSE 0 END) AS affected_mvp_transactions
        FROM tmp_ambiguous_candidates a
        JOIN tmp_ambiguous_candidates b
          ON a.transaction_id = b.transaction_id
         AND a.mapping_id < b.mapping_id
         AND a.candidate_ticker = b.candidate_ticker
         AND a.valid_from <= b.valid_to
         AND b.valid_from <= a.valid_to
        GROUP BY a.issuer_cik, a.candidate_ticker, a.mapping_id, b.mapping_id
        ORDER BY affected_mvp_transactions DESC
        LIMIT 5000
        """,
        "ambiguity_overlapping_intervals.csv",
    )
    export_query(conn, "SELECT * FROM tmp_ambiguous_summary WHERE involves_review = 1 LIMIT 5000", "ambiguity_share_class_candidates.csv")
    export_query(conn, "SELECT * FROM tmp_ambiguous_summary WHERE logical_candidate_count = 1 AND raw_candidate_count > 1 LIMIT 5000", "ambiguity_duplicate_equivalent_candidates.csv")
    export_query(
        conn,
        """
        SELECT rule,
               resolved_transactions,
               resolved_mvp_transactions,
               ROUND(100.0 * resolved_mvp_transactions / NULLIF((SELECT mvp_transactions FROM tmp_mapping_totals), 0), 4) AS mvp_coverage_points
        FROM (
            SELECT 'BASELINE_AMBIGUOUS' AS rule, COUNT(*) AS resolved_transactions, SUM(include_mvp_alpha) AS resolved_mvp_transactions FROM tmp_ambiguous_summary WHERE 0
            UNION ALL
            SELECT 'COLLAPSE_EQUIVALENT', COUNT(*), SUM(include_mvp_alpha) FROM tmp_ambiguous_summary WHERE logical_candidate_count = 1 AND involves_mapping_ambiguity = 0
            UNION ALL
            SELECT 'EXACT_TRANSACTION_TICKER', COUNT(*), SUM(include_mvp_alpha) FROM tmp_ambiguous_summary WHERE logical_candidate_count > 1 AND exact_ticker_logical_count = 1 AND involves_mapping_ambiguity = 0
            UNION ALL
            SELECT 'FINAL_DETERMINISTIC', COUNT(*), SUM(include_mvp_alpha) FROM tmp_ambiguous_summary WHERE involves_mapping_ambiguity = 0 AND (logical_candidate_count = 1 OR exact_ticker_logical_count = 1)
        )
        """,
        "ambiguity_resolution_simulation.csv",
    )
    export_query(conn, "SELECT * FROM tmp_ambiguous_summary ORDER BY include_mvp_alpha DESC, transaction_value DESC LIMIT 5000", "ambiguous_mvp_diagnostic_sample.csv")
    export_query(conn, "SELECT issuer_cik, COUNT(*) AS ambiguous_transactions, SUM(include_mvp_alpha) AS ambiguous_mvp_transactions FROM tmp_ambiguous_summary GROUP BY issuer_cik ORDER BY ambiguous_mvp_transactions DESC LIMIT 100", "top_ambiguous_issuers.csv")
    logger.info("ambiguity analysis complete in %.2fs", time.perf_counter() - start)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze Phase 5.1 mapping ambiguity.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stdout,
    )
    conn = get_connection(args.db_path)
    try:
        analyze(conn, args.limit)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
