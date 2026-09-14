"""Build Phase 5 historical security mappings.

This is a derived layer. It never mutates raw SEC, clean transactions, or
Phase 4 entity tables.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.cleaning.clean_logic import classify_ticker
from src.utils.db import PROJECT_ROOT, execute_sql_file, get_connection, table_exists

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "insider_research.db"
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "sql" / "004_create_market_data_tables.sql"
EXPORT_DIR = PROJECT_ROOT / "data" / "exports" / "security_mapping"
BUILDER_VERSION = "phase5-security-mapping-2026-08-14"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mapping_id_for(
    issuer_cik: str,
    ticker: str,
    security_title: str,
    valid_from: str,
    valid_to: str,
) -> str:
    payload = "|".join(
        [
            issuer_cik or "",
            ticker or "",
            security_title or "",
            valid_from or "",
            valid_to or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    execute_sql_file(conn, DEFAULT_SCHEMA_PATH)
    _ensure_transaction_mapping_columns(conn)


def _ensure_transaction_mapping_columns(conn: sqlite3.Connection) -> None:
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(transaction_market_mapping)").fetchall()
    }
    columns = {
        "mapping_status": "TEXT",
        "candidate_count_raw": "INTEGER",
        "candidate_count_logical": "INTEGER",
        "resolution_method": "TEXT",
        "ambiguity_reason": "TEXT",
        "source_ticker_match": "INTEGER",
    }
    for column, column_type in columns.items():
        if column not in existing:
            conn.execute(
                f"ALTER TABLE transaction_market_mapping ADD COLUMN {column} {column_type}"
            )
    conn.commit()


def _table_required(conn: sqlite3.Connection, table: str) -> None:
    if not table_exists(conn, table):
        raise RuntimeError(f"{table} not found. Run previous phases first.")


def reset_mapping_tables(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM transaction_market_mapping")
    conn.execute("DELETE FROM security_identifier_mapping")
    conn.commit()


def start_build_metadata(conn: sqlite3.Connection, *, limit: int | None) -> str:
    build_id = uuid.uuid4().hex
    source_rows = conn.execute("SELECT COUNT(*) FROM company_identifier_history").fetchone()[0]
    conn.execute(
        """
        INSERT INTO security_mapping_build_metadata (
            build_id, started_at, completed_at, status, source_identifier_row_count,
            mapping_row_count, transaction_mapping_row_count, builder_version,
            is_sample, sample_limit, error_message
        ) VALUES (?, ?, NULL, ?, ?, 0, 0, ?, ?, ?, NULL)
        """,
        (
            build_id,
            utc_now(),
            "SAMPLE" if limit else "IN_PROGRESS",
            source_rows,
            BUILDER_VERSION,
            1 if limit else 0,
            limit,
        ),
    )
    conn.commit()
    return build_id


def finish_build_metadata(
    conn: sqlite3.Connection,
    build_id: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    mapping_rows = conn.execute("SELECT COUNT(*) FROM security_identifier_mapping").fetchone()[0]
    tx_rows = conn.execute("SELECT COUNT(*) FROM transaction_market_mapping").fetchone()[0]
    conn.execute(
        """
        UPDATE security_mapping_build_metadata
        SET completed_at = ?, status = ?, mapping_row_count = ?,
            transaction_mapping_row_count = ?, error_message = ?
        WHERE build_id = ?
        """,
        (utc_now(), status, mapping_rows, tx_rows, error, build_id),
    )
    conn.commit()


def register_mapping_functions(conn: sqlite3.Connection) -> None:
    conn.create_function("security_mapping_id", 5, mapping_id_for)


def build_security_identifier_mapping(conn: sqlite3.Connection) -> None:
    now = utc_now()
    conn.execute(
        """
        DROP TABLE IF EXISTS temp.tmp_security_title_by_issuer_ticker
        """
    )
    conn.execute(
        """
        CREATE TEMP TABLE tmp_security_title_by_issuer_ticker AS
        SELECT issuer_cik,
               ticker_clean,
               COALESCE(NULLIF(TRIM(security_title_raw), ''), '') AS security_title,
               COUNT(*) AS occurrence_count,
               SUM(CASE WHEN include_mvp_alpha = 1 THEN 1 ELSE 0 END) AS mvp_transaction_count,
               ROW_NUMBER() OVER (
                   PARTITION BY issuer_cik, ticker_clean
                   ORDER BY SUM(CASE WHEN include_mvp_alpha = 1 THEN 1 ELSE 0 END) DESC,
                            COUNT(*) DESC,
                            COALESCE(NULLIF(TRIM(security_title_raw), ''), '') ASC
               ) AS rn
        FROM clean_insider_transactions
        WHERE issuer_cik IS NOT NULL AND TRIM(issuer_cik) <> ''
          AND ticker_clean IS NOT NULL AND TRIM(ticker_clean) <> ''
        GROUP BY issuer_cik, ticker_clean, security_title
        """
    )
    conn.execute(
        """
        DROP TABLE IF EXISTS temp.tmp_security_title_counts
        """
    )
    conn.execute(
        """
        CREATE TEMP TABLE tmp_security_title_counts AS
        SELECT issuer_cik, ticker_clean, COUNT(*) AS security_title_count
        FROM tmp_security_title_by_issuer_ticker
        WHERE security_title <> ''
        GROUP BY issuer_cik, ticker_clean
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tmp_sec_title_key ON tmp_security_title_by_issuer_ticker (issuer_cik, ticker_clean, rn)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tmp_sec_title_count_key ON tmp_security_title_counts (issuer_cik, ticker_clean)")
    conn.execute(
        """
        INSERT INTO security_identifier_mapping (
            mapping_id, issuer_cik, ticker_raw, ticker_clean, market_data_symbol,
            security_title, security_class, valid_from, valid_to, exchange,
            mapping_source, mapping_method, mapping_confidence, is_primary_mapping,
            is_current, is_delisted, is_ticker_change, is_reused_ticker,
            is_ambiguous, requires_review, source_first_seen_date,
            source_last_seen_date, created_at, updated_at
        )
        WITH source_hist AS (
            SELECT
                issuer_cik,
                ticker,
                MIN(company_name) AS representative_company_name,
                first_seen_date,
                last_seen_date,
                MIN(first_source_year) AS first_source_year,
                MAX(last_source_year) AS last_source_year,
                SUM(occurrence_count) AS occurrence_count,
                MAX(is_latest_ticker) AS is_latest_ticker,
                MAX(is_latest_name) AS is_latest_name,
                MAX(ticker_is_valid) AS ticker_is_valid,
                MAX(ticker_is_multisymbol) AS ticker_is_multisymbol,
                COUNT(*) AS source_row_count,
                COUNT(DISTINCT COALESCE(company_name, '')) AS source_company_name_count
            FROM company_identifier_history
            WHERE issuer_cik IS NOT NULL AND TRIM(issuer_cik) <> ''
              AND ticker IS NOT NULL AND TRIM(ticker) <> ''
            GROUP BY issuer_cik, ticker, first_seen_date, last_seen_date
        ),
        issuer_counts AS (
            SELECT issuer_cik, COUNT(DISTINCT ticker) AS issuer_ticker_count
            FROM source_hist
            GROUP BY issuer_cik
        ),
        ticker_counts AS (
            SELECT ticker, COUNT(DISTINCT issuer_cik) AS ticker_issuer_count
            FROM source_hist
            GROUP BY ticker
        ),
        hist AS (
            SELECT h.*,
                   ic.issuer_ticker_count,
                   tc.ticker_issuer_count
                FROM source_hist h
                JOIN issuer_counts ic ON h.issuer_cik = ic.issuer_cik
                JOIN ticker_counts tc ON h.ticker = tc.ticker
            )
        SELECT
            security_mapping_id(
                hist.issuer_cik,
                hist.ticker,
                COALESCE(title.security_title, ''),
                hist.first_seen_date,
                hist.last_seen_date
            ) AS mapping_id,
            hist.issuer_cik,
            hist.ticker AS ticker_raw,
            hist.ticker AS ticker_clean,
            CASE WHEN hist.ticker_is_valid = 1 THEN hist.ticker ELSE NULL END AS market_data_symbol,
            NULLIF(title.security_title, '') AS security_title,
            NULL AS security_class,
            hist.first_seen_date AS valid_from,
            hist.last_seen_date AS valid_to,
            NULL AS exchange,
            'SEC_DERIVED' AS mapping_source,
            'OBSERVED_INTERVAL' AS mapping_method,
            CASE
                WHEN hist.ticker_is_valid = 1
                 AND COALESCE(title_counts.security_title_count, 0) <= 1
                THEN 'HIGH'
                WHEN hist.ticker_is_valid = 1 THEN 'MEDIUM'
                ELSE 'LOW'
            END AS mapping_confidence,
            CASE WHEN hist.is_latest_ticker = 1 AND hist.ticker_is_valid = 1 THEN 1 ELSE 0 END AS is_primary_mapping,
            hist.is_latest_ticker AS is_current,
            CASE WHEN hist.is_latest_ticker = 0 THEN 1 ELSE 0 END AS is_delisted,
            CASE WHEN hist.issuer_ticker_count > 1 THEN 1 ELSE 0 END AS is_ticker_change,
            CASE WHEN hist.ticker_issuer_count > 1 THEN 1 ELSE 0 END AS is_reused_ticker,
            CASE
                WHEN hist.ticker_is_valid != 1
                  OR COALESCE(title_counts.security_title_count, 0) > 1
                THEN 1 ELSE 0
            END AS is_ambiguous,
            CASE
                WHEN hist.ticker_is_valid != 1
                  OR hist.source_row_count > 1
                  OR COALESCE(title_counts.security_title_count, 0) > 1
                THEN 1 ELSE 0
            END AS requires_review,
            hist.first_seen_date,
            hist.last_seen_date,
            ?,
            ?
        FROM hist
        LEFT JOIN tmp_security_title_by_issuer_ticker title
          ON hist.issuer_cik = title.issuer_cik
         AND hist.ticker = title.ticker_clean
         AND title.rn = 1
        LEFT JOIN tmp_security_title_counts title_counts
          ON hist.issuer_cik = title_counts.issuer_cik
         AND hist.ticker = title_counts.ticker_clean
        """,
        (now, now),
    )
    conn.commit()


def build_transaction_market_mapping(conn: sqlite3.Connection, limit: int | None = None) -> None:
    now = utc_now()
    conn.execute("DROP TABLE IF EXISTS temp.tmp_transaction_scope")
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    conn.execute(
        f"""
        CREATE TEMP TABLE tmp_transaction_scope AS
        SELECT transaction_id, issuer_cik, transaction_date, filing_date,
               ticker_clean, include_mvp_alpha, transaction_code
        FROM clean_insider_transactions
        ORDER BY rowid
        {limit_sql}
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tmp_transaction_scope_id ON tmp_transaction_scope (transaction_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tmp_transaction_scope_lookup ON tmp_transaction_scope (issuer_cik, transaction_date)")
    conn.execute("DROP TABLE IF EXISTS temp.tmp_transaction_candidates")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_transaction_candidates AS
        SELECT t.transaction_id,
               t.issuer_cik,
               t.transaction_date,
               t.filing_date,
               t.ticker_clean AS transaction_ticker_clean,
               m.ticker_clean,
               m.market_data_symbol,
               m.mapping_id,
               m.mapping_confidence,
               m.mapping_method,
               m.is_ambiguous AS mapping_is_ambiguous,
               m.requires_review AS mapping_requires_review
        FROM tmp_transaction_scope t
        JOIN security_identifier_mapping m
          ON t.issuer_cik = m.issuer_cik
         AND t.transaction_date IS NOT NULL
         AND m.valid_from IS NOT NULL
         AND m.valid_to IS NOT NULL
         AND t.transaction_date >= m.valid_from
         AND t.transaction_date <= m.valid_to
        WHERE m.market_data_symbol IS NOT NULL
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tmp_candidates_tid ON tmp_transaction_candidates (transaction_id)")
    conn.execute("DROP TABLE IF EXISTS temp.tmp_transaction_logical_candidates")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_transaction_logical_candidates AS
        SELECT transaction_id,
               issuer_cik,
               transaction_date,
               filing_date,
               transaction_ticker_clean,
               ticker_clean,
               market_data_symbol,
               MIN(mapping_id) AS mapping_id,
               MIN(mapping_confidence) AS mapping_confidence,
               MIN(mapping_method) AS mapping_method,
               MAX(mapping_is_ambiguous) AS mapping_is_ambiguous,
               MAX(mapping_requires_review) AS mapping_requires_review,
               COUNT(*) AS raw_candidate_count
        FROM tmp_transaction_candidates
        GROUP BY transaction_id, issuer_cik, transaction_date, filing_date,
                 transaction_ticker_clean, ticker_clean, market_data_symbol
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tmp_logical_candidates_tid ON tmp_transaction_logical_candidates (transaction_id)")
    conn.execute("DROP TABLE IF EXISTS temp.tmp_transaction_candidate_summary")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_transaction_candidate_summary AS
        SELECT transaction_id,
               SUM(raw_candidate_count) AS candidate_count_raw,
               COUNT(*) AS candidate_count_logical,
               SUM(CASE WHEN ticker_clean = transaction_ticker_clean THEN 1 ELSE 0 END) AS exact_ticker_logical_count,
               MAX(mapping_is_ambiguous) AS any_mapping_ambiguous,
               MAX(mapping_requires_review) AS any_requires_review
        FROM tmp_transaction_logical_candidates
        GROUP BY transaction_id
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tmp_candidate_summary_tid ON tmp_transaction_candidate_summary (transaction_id)")
    conn.execute("DROP TABLE IF EXISTS temp.tmp_transaction_unique_logical")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_transaction_unique_logical AS
        SELECT *
        FROM tmp_transaction_logical_candidates
        GROUP BY transaction_id
        HAVING COUNT(*) = 1
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tmp_unique_logical_tid ON tmp_transaction_unique_logical (transaction_id)")
    conn.execute("DROP TABLE IF EXISTS temp.tmp_transaction_unique_ticker_match")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_transaction_unique_ticker_match AS
        SELECT *
        FROM tmp_transaction_logical_candidates
        WHERE ticker_clean = transaction_ticker_clean
        GROUP BY transaction_id
        HAVING COUNT(*) = 1
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tmp_unique_ticker_match_tid ON tmp_transaction_unique_ticker_match (transaction_id)")
    conn.execute(
        """
        INSERT INTO transaction_market_mapping (
            transaction_id, issuer_cik, transaction_date, filing_date,
            ticker_at_transaction, market_data_symbol, mapping_id,
            mapping_confidence, mapping_method, mapping_status,
            candidate_count_raw, candidate_count_logical, resolution_method,
            ambiguity_reason, source_ticker_match, is_mapped, is_ambiguous,
            requires_review, created_at
        )
        SELECT
            t.transaction_id,
            t.issuer_cik,
            t.transaction_date,
            t.filing_date,
            CASE WHEN COALESCE(s.candidate_count_logical, 0) = 1 AND COALESCE(u.mapping_is_ambiguous, 0) = 0 THEN u.ticker_clean
                 WHEN COALESCE(s.exact_ticker_logical_count, 0) = 1 AND COALESCE(x.mapping_is_ambiguous, 0) = 0 THEN x.ticker_clean
                 ELSE NULL END,
            CASE WHEN COALESCE(s.candidate_count_logical, 0) = 1 AND COALESCE(u.mapping_is_ambiguous, 0) = 0 THEN u.market_data_symbol
                 WHEN COALESCE(s.exact_ticker_logical_count, 0) = 1 AND COALESCE(x.mapping_is_ambiguous, 0) = 0 THEN x.market_data_symbol
                 ELSE NULL END,
            CASE WHEN COALESCE(s.candidate_count_logical, 0) = 1 AND COALESCE(u.mapping_is_ambiguous, 0) = 0 THEN u.mapping_id
                 WHEN COALESCE(s.exact_ticker_logical_count, 0) = 1 AND COALESCE(x.mapping_is_ambiguous, 0) = 0 THEN x.mapping_id
                 ELSE NULL END,
            CASE WHEN COALESCE(s.candidate_count_logical, 0) = 1 AND COALESCE(u.mapping_is_ambiguous, 0) = 0 THEN u.mapping_confidence
                 WHEN COALESCE(s.exact_ticker_logical_count, 0) = 1 AND COALESCE(x.mapping_is_ambiguous, 0) = 0 THEN x.mapping_confidence
                 ELSE NULL END,
            CASE WHEN COALESCE(s.candidate_count_logical, 0) = 1 AND COALESCE(u.mapping_is_ambiguous, 0) = 0 THEN u.mapping_method
                 WHEN COALESCE(s.exact_ticker_logical_count, 0) = 1 AND COALESCE(x.mapping_is_ambiguous, 0) = 0 THEN x.mapping_method
                 ELSE NULL END,
            CASE WHEN COALESCE(s.candidate_count_logical, 0) = 0 THEN 'UNMAPPED'
                 WHEN (COALESCE(s.candidate_count_logical, 0) = 1 AND COALESCE(u.mapping_is_ambiguous, 0) = 0)
                   OR (COALESCE(s.exact_ticker_logical_count, 0) = 1 AND COALESCE(x.mapping_is_ambiguous, 0) = 0) THEN 'MAPPED'
                 ELSE 'AMBIGUOUS' END,
            COALESCE(s.candidate_count_raw, 0),
            COALESCE(s.candidate_count_logical, 0),
            CASE WHEN COALESCE(s.candidate_count_logical, 0) = 1 AND COALESCE(s.candidate_count_raw, 0) > 1 AND COALESCE(u.mapping_is_ambiguous, 0) = 0 THEN 'COLLAPSED_EQUIVALENT_CANDIDATES'
                 WHEN COALESCE(s.candidate_count_logical, 0) = 1 AND COALESCE(u.mapping_is_ambiguous, 0) = 0 THEN 'CIK_DATE_UNIQUE'
                 WHEN COALESCE(s.exact_ticker_logical_count, 0) = 1 AND COALESCE(x.mapping_is_ambiguous, 0) = 0 THEN 'CIK_DATE_TICKER_EXACT'
                 ELSE NULL END,
            CASE WHEN COALESCE(s.candidate_count_logical, 0) = 0 THEN 'NO_DATE_VALID_CANDIDATE'
                 WHEN COALESCE(s.any_mapping_ambiguous, 0) = 1 THEN 'MAPPING_REQUIRES_REVIEW'
                 WHEN COALESCE(s.exact_ticker_logical_count, 0) > 1 THEN 'MULTIPLE_EXACT_TICKER_CANDIDATES'
                 WHEN COALESCE(s.exact_ticker_logical_count, 0) = 0 AND COALESCE(s.candidate_count_logical, 0) > 1 THEN 'NO_EXACT_TICKER_MATCH'
                 WHEN COALESCE(s.candidate_count_logical, 0) > 1 THEN 'MULTIPLE_LOGICAL_CANDIDATES'
                 ELSE NULL END,
            CASE WHEN COALESCE(s.exact_ticker_logical_count, 0) > 0 THEN 1 ELSE 0 END,
            CASE WHEN (COALESCE(s.candidate_count_logical, 0) = 1 AND COALESCE(u.mapping_is_ambiguous, 0) = 0)
                   OR (COALESCE(s.exact_ticker_logical_count, 0) = 1 AND COALESCE(x.mapping_is_ambiguous, 0) = 0) THEN 1 ELSE 0 END,
            CASE WHEN COALESCE(s.candidate_count_logical, 0) > 0
                   AND NOT ((COALESCE(s.candidate_count_logical, 0) = 1 AND COALESCE(u.mapping_is_ambiguous, 0) = 0)
                   OR (COALESCE(s.exact_ticker_logical_count, 0) = 1 AND COALESCE(x.mapping_is_ambiguous, 0) = 0)) THEN 1 ELSE 0 END,
            CASE WHEN COALESCE(s.candidate_count_logical, 0) = 0
                   OR COALESCE(s.any_requires_review, 0) = 1
                   OR NOT ((COALESCE(s.candidate_count_logical, 0) = 1 AND COALESCE(u.mapping_is_ambiguous, 0) = 0)
                   OR (COALESCE(s.exact_ticker_logical_count, 0) = 1 AND COALESCE(x.mapping_is_ambiguous, 0) = 0)) THEN 1 ELSE 0 END,
            ?
        FROM tmp_transaction_scope t
        LEFT JOIN tmp_transaction_candidate_summary s
          ON t.transaction_id = s.transaction_id
        LEFT JOIN tmp_transaction_unique_logical u
          ON t.transaction_id = u.transaction_id
        LEFT JOIN tmp_transaction_unique_ticker_match x
          ON t.transaction_id = x.transaction_id
        """,
        (now,),
    )
    conn.commit()


def export_query(conn: sqlite3.Connection, sql: str, filename: str) -> pd.DataFrame:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_sql_query(sql, conn)
    df.to_csv(EXPORT_DIR / filename, index=False)
    logger.info("exported %s (%s rows)", filename, len(df))
    return df


def export_mapping_qa(conn: sqlite3.Connection) -> None:
    export_query(
        conn,
        """
        WITH source_hist AS (
            SELECT issuer_cik, ticker, first_seen_date, last_seen_date,
                   COUNT(*) AS source_row_count,
                   COUNT(DISTINCT COALESCE(company_name, '')) AS company_name_count,
                   GROUP_CONCAT(DISTINCT company_name) AS company_names
            FROM company_identifier_history
            WHERE issuer_cik IS NOT NULL AND TRIM(issuer_cik) <> ''
              AND ticker IS NOT NULL AND TRIM(ticker) <> ''
            GROUP BY issuer_cik, ticker, first_seen_date, last_seen_date
            HAVING COUNT(*) > 1
        )
        SELECT * FROM source_hist
        ORDER BY source_row_count DESC, issuer_cik, ticker
        """,
        "duplicate_source_identifier_records.csv",
    )
    export_query(
        conn,
        """
        SELECT 'mapping_rows' AS metric, COUNT(*) AS value FROM security_identifier_mapping
        UNION ALL SELECT 'accepted_mappings', COUNT(*) FROM security_identifier_mapping WHERE is_ambiguous = 0 AND market_data_symbol IS NOT NULL
        UNION ALL SELECT 'ambiguous_mappings', COUNT(*) FROM security_identifier_mapping WHERE is_ambiguous = 1
        UNION ALL SELECT 'ticker_change_cases', COUNT(DISTINCT issuer_cik) FROM security_identifier_mapping WHERE is_ticker_change = 1
        UNION ALL SELECT 'ticker_reuse_cases', COUNT(DISTINCT ticker_clean) FROM security_identifier_mapping WHERE is_reused_ticker = 1
        """,
        "security_mapping_summary.csv",
    )
    export_query(
        conn,
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
        "transaction_mapping_coverage.csv",
    )
    export_query(
        conn,
        """
        SELECT tm.*, t.transaction_value, t.insider_cik
        FROM transaction_market_mapping tm
        JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
        WHERE t.include_mvp_alpha = 1 AND tm.is_mapped = 0 AND tm.is_ambiguous = 0
        ORDER BY t.transaction_value DESC
        LIMIT 5000
        """,
        "unmapped_mvp_transactions.csv",
    )
    export_query(
        conn,
        """
        SELECT tm.*, t.transaction_value, t.insider_cik
        FROM transaction_market_mapping tm
        JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
        WHERE t.include_mvp_alpha = 1 AND tm.is_ambiguous = 1
        ORDER BY t.transaction_value DESC
        LIMIT 5000
        """,
        "ambiguous_mvp_transactions.csv",
    )
    export_query(
        conn,
        "SELECT * FROM company_dimension WHERE distinct_ticker_count > 1 ORDER BY mvp_transaction_count DESC",
        "companies_multiple_tickers.csv",
    )
    export_query(
        conn,
        """
        SELECT ticker_clean AS ticker, COUNT(DISTINCT issuer_cik) AS issuer_cik_count,
               GROUP_CONCAT(DISTINCT issuer_cik) AS issuer_ciks
        FROM security_identifier_mapping
        WHERE ticker_clean IS NOT NULL
        GROUP BY ticker_clean
        HAVING COUNT(DISTINCT issuer_cik) > 1
        ORDER BY issuer_cik_count DESC
        """,
        "tickers_multiple_ciks.csv",
    )
    export_query(
        conn,
        """
        SELECT issuer_cik, COUNT(DISTINCT ticker_clean) AS ticker_count,
               GROUP_CONCAT(DISTINCT ticker_clean) AS tickers
        FROM security_identifier_mapping
        GROUP BY issuer_cik
        HAVING COUNT(DISTINCT ticker_clean) > 1
        ORDER BY ticker_count DESC
        """,
        "ticker_change_candidates.csv",
    )
    export_query(
        conn,
        "SELECT * FROM security_identifier_mapping WHERE is_delisted = 1 ORDER BY source_last_seen_date DESC",
        "delisted_security_candidates.csv",
    )
    export_query(
        conn,
        "SELECT * FROM security_identifier_mapping WHERE is_ambiguous = 1 OR requires_review = 1",
        "share_class_review_candidates.csv",
    )


def build_security_mapping(
    conn: sqlite3.Connection,
    *,
    reset: bool = False,
    limit: int | None = None,
    export: bool = True,
) -> None:
    _table_required(conn, "company_identifier_history")
    _table_required(conn, "company_dimension")
    _table_required(conn, "clean_insider_transactions")
    ensure_schema(conn)
    register_mapping_functions(conn)
    build_id = start_build_metadata(conn, limit=limit)
    start = time.perf_counter()
    try:
        if reset:
            reset_mapping_tables(conn)
        logger.info("Building security_identifier_mapping")
        build_security_identifier_mapping(conn)
        logger.info("security_identifier_mapping rows=%s", conn.execute("SELECT COUNT(*) FROM security_identifier_mapping").fetchone()[0])
        logger.info("Building transaction_market_mapping limit=%s", limit)
        build_transaction_market_mapping(conn, limit=limit)
        logger.info("transaction_market_mapping rows=%s", conn.execute("SELECT COUNT(*) FROM transaction_market_mapping").fetchone()[0])
        if export:
            export_mapping_qa(conn)
        finish_build_metadata(conn, build_id, "SAMPLE" if limit else "COMPLETE")
        logger.info("Security mapping build complete in %.2fs", time.perf_counter() - start)
    except Exception as exc:
        finish_build_metadata(conn, build_id, "FAILED", error=str(exc))
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Phase 5 historical security mapping.")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=None, help="Limit transaction mapping rows for smoke tests.")
    parser.add_argument("--no-export", action="store_true")
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
        build_security_mapping(conn, reset=args.reset, limit=args.limit, export=not args.no_export)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
