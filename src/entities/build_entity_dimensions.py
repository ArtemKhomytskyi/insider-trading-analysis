"""Build Phase 4 entity dimensions from clean_insider_transactions.

Usage:
    python -m src.entities.build_entity_dimensions --reset
    python -m src.entities.build_entity_dimensions --reset --limit 100000
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.cleaning.clean_logic import normalize_insider_name
from src.utils.db import PROJECT_ROOT, execute_sql_file, get_connection, table_exists

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "insider_research.db"
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "sql" / "003_create_entity_dimensions.sql"
EXPORT_DIR = PROJECT_ROOT / "data" / "exports" / "entity_normalization"
BUILDER_VERSION = "phase4-optimized-2026-08-13"

ENTITY_TABLES = (
    "clean_insiders",
    "insider_name_aliases",
    "company_dimension",
    "company_identifier_history",
    "insider_company_roles",
)
BUILD_PREFIX = "entity_build_"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def qname(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def scalar(conn: sqlite3.Connection, sql: str, params: tuple | None = None) -> int:
    row = conn.execute(sql, params or ()).fetchone()
    return int(row[0] or 0)


class StepTimer:
    def __init__(self) -> None:
        self.started = time.perf_counter()

    def elapsed(self) -> float:
        return time.perf_counter() - self.started


def log_step(step: int, total: int, label: str) -> StepTimer:
    logger.info("[%s/%s] %s...", step, total, label)
    return StepTimer()


def log_done(label: str, timer: StepTimer, count: int | None = None) -> None:
    suffix = f" rows={count:,}" if count is not None else ""
    logger.info("      %s done in %.2fs%s", label, timer.elapsed(), suffix)


def ensure_entity_schema(conn: sqlite3.Connection, schema_path: Path = DEFAULT_SCHEMA_PATH) -> None:
    execute_sql_file(conn, schema_path)


def drop_build_tables(conn: sqlite3.Connection) -> None:
    for table in ENTITY_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {qname(BUILD_PREFIX + table)}")
    conn.commit()


def create_build_tables(conn: sqlite3.Connection) -> None:
    for table in ENTITY_TABLES:
        build = BUILD_PREFIX + table
        conn.execute(f"DROP TABLE IF EXISTS {qname(build)}")
        conn.execute(f"CREATE TABLE {qname(build)} AS SELECT * FROM {qname(table)} WHERE 0")
    conn.commit()


def create_temp_source(conn: sqlite3.Connection, limit: int | None) -> None:
    conn.execute("DROP TABLE IF EXISTS temp.tmp_entity_source")
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    conn.execute(
        f"""
        CREATE TEMP TABLE tmp_entity_source AS
        SELECT
            rowid AS source_rowid,
            transaction_id,
            accession_number,
            issuer_cik,
            issuer_name,
            ticker_clean,
            is_valid_ticker,
            is_multisymbol_ticker,
            insider_cik,
            insider_name,
            insider_name_clean,
            raw_relationship,
            raw_role_title,
            normalized_role,
            role_group,
            role_weight,
            is_executive,
            is_board_member,
            is_large_owner,
            role_requires_review,
            role_confidence,
            transaction_date,
            filing_date,
            transaction_code,
            is_purchase,
            is_sale,
            include_mvp_alpha,
            source_year
        FROM clean_insider_transactions
        ORDER BY rowid
        {limit_sql}
        """
    )
    indexes = (
        "CREATE INDEX idx_tmp_entity_source_insider ON tmp_entity_source (insider_cik)",
        "CREATE INDEX idx_tmp_entity_source_company ON tmp_entity_source (issuer_cik)",
        "CREATE INDEX idx_tmp_entity_source_insider_company ON tmp_entity_source (insider_cik, issuer_cik)",
        "CREATE INDEX idx_tmp_entity_source_name ON tmp_entity_source (insider_name)",
        "CREATE INDEX idx_tmp_entity_source_role ON tmp_entity_source (insider_cik, normalized_role, role_group)",
        "CREATE INDEX idx_tmp_entity_source_company_hist ON tmp_entity_source (issuer_cik, ticker_clean, issuer_name)",
    )
    for sql in indexes:
        conn.execute(sql)
    conn.commit()


def create_name_normalization_map(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT DISTINCT insider_name
        FROM tmp_entity_source
        WHERE insider_name IS NOT NULL AND TRIM(insider_name) <> ''
        """
    ).fetchall()
    conn.execute("DROP TABLE IF EXISTS temp.tmp_insider_name_normalization")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_insider_name_normalization (
            raw_name TEXT PRIMARY KEY,
            normalized_name TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO tmp_insider_name_normalization VALUES (?, ?)",
        [(row[0], normalize_insider_name(row[0])) for row in rows],
    )
    conn.execute(
        "CREATE INDEX idx_tmp_insider_name_norm_raw ON tmp_insider_name_normalization (raw_name)"
    )
    conn.commit()
    logger.info("      distinct raw insider names: %s", len(rows))


def build_insider_aliases(conn: sqlite3.Connection, target: str) -> None:
    conn.execute(
        f"""
        INSERT INTO {qname(target)} (
            insider_cik, raw_name, normalized_name, first_seen_date, last_seen_date,
            occurrence_count, is_canonical
        )
        WITH aliases AS (
            SELECT
                s.insider_cik,
                COALESCE(NULLIF(TRIM(s.insider_name), ''), '') AS raw_name,
                COALESCE(NULLIF(TRIM(s.insider_name_clean), ''), n.normalized_name, '') AS normalized_name,
                MIN(s.transaction_date) AS first_seen_date,
                MAX(s.transaction_date) AS last_seen_date,
                COUNT(*) AS occurrence_count
            FROM tmp_entity_source s
            LEFT JOIN tmp_insider_name_normalization n ON s.insider_name = n.raw_name
            WHERE s.insider_cik IS NOT NULL AND TRIM(s.insider_cik) <> ''
            GROUP BY s.insider_cik, raw_name, normalized_name
        ),
        chosen AS (
            SELECT insider_cik, raw_name, normalized_name
            FROM (
                SELECT aliases.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY insider_cik
                           ORDER BY CASE WHEN raw_name <> '' THEN 0 ELSE 1 END,
                                    last_seen_date DESC, occurrence_count DESC,
                                    normalized_name ASC, raw_name ASC
                       ) AS rn
                FROM aliases
            )
            WHERE rn = 1
        )
        SELECT aliases.insider_cik, aliases.raw_name, aliases.normalized_name,
               aliases.first_seen_date, aliases.last_seen_date, aliases.occurrence_count,
               CASE WHEN aliases.raw_name = chosen.raw_name
                      AND aliases.normalized_name = chosen.normalized_name
                    THEN 1 ELSE 0 END
        FROM aliases
        JOIN chosen ON aliases.insider_cik = chosen.insider_cik
        """
    )
    conn.commit()


def build_clean_insiders(conn: sqlite3.Connection, target: str, alias_table: str) -> None:
    now = utc_now()
    conn.executescript(
        """
        DROP TABLE IF EXISTS temp.tmp_insider_agg;
        CREATE TEMP TABLE tmp_insider_agg AS
        SELECT insider_cik,
               MIN(transaction_date) AS first_seen_date,
               MAX(transaction_date) AS last_seen_date,
               MIN(filing_date) AS first_filing_date,
               MAX(filing_date) AS last_filing_date,
               COUNT(*) AS transaction_count,
               SUM(CASE WHEN is_purchase = 1 THEN 1 ELSE 0 END) AS purchase_count,
               SUM(CASE WHEN is_sale = 1 THEN 1 ELSE 0 END) AS sale_count,
               SUM(CASE WHEN include_mvp_alpha = 1 THEN 1 ELSE 0 END) AS mvp_transaction_count,
               MAX(CASE WHEN is_board_member = 1 OR raw_relationship LIKE '%Director%' THEN 1 ELSE 0 END) AS is_ever_director,
               MAX(CASE WHEN is_executive = 1 OR raw_relationship LIKE '%Officer%' THEN 1 ELSE 0 END) AS is_ever_officer,
               MAX(CASE WHEN is_large_owner = 1 OR raw_relationship LIKE '%TenPercentOwner%' THEN 1 ELSE 0 END) AS is_ever_ten_percent_owner
        FROM tmp_entity_source
        WHERE insider_cik IS NOT NULL AND TRIM(insider_cik) <> ''
        GROUP BY insider_cik;
        CREATE INDEX idx_tmp_insider_agg_cik ON tmp_insider_agg (insider_cik);

        DROP TABLE IF EXISTS temp.tmp_insider_filings;
        CREATE TEMP TABLE tmp_insider_filings AS
        SELECT insider_cik, COUNT(*) AS filing_count
        FROM (
            SELECT DISTINCT insider_cik, accession_number
            FROM clean_insider_transactions
            WHERE insider_cik IS NOT NULL AND TRIM(insider_cik) <> ''
              AND accession_number IS NOT NULL
        )
        GROUP BY insider_cik;
        CREATE INDEX idx_tmp_insider_filings_cik ON tmp_insider_filings (insider_cik);

        DROP TABLE IF EXISTS temp.tmp_insider_companies;
        CREATE TEMP TABLE tmp_insider_companies AS
        SELECT insider_cik, COUNT(*) AS company_count
        FROM (
            SELECT DISTINCT insider_cik, issuer_cik
            FROM clean_insider_transactions
            WHERE insider_cik IS NOT NULL AND TRIM(insider_cik) <> ''
              AND issuer_cik IS NOT NULL AND TRIM(issuer_cik) <> ''
        )
        GROUP BY insider_cik;
        CREATE INDEX idx_tmp_insider_companies_cik ON tmp_insider_companies (insider_cik);

        DROP TABLE IF EXISTS temp.tmp_insider_roles;
        CREATE TEMP TABLE tmp_insider_roles AS
        SELECT insider_cik, normalized_role, role_group,
               COUNT(*) AS role_count, MAX(transaction_date) AS last_role_date
        FROM tmp_entity_source
        WHERE insider_cik IS NOT NULL AND TRIM(insider_cik) <> ''
          AND normalized_role IS NOT NULL AND TRIM(normalized_role) <> ''
        GROUP BY insider_cik, normalized_role, role_group;
        CREATE INDEX idx_tmp_insider_roles_cik ON tmp_insider_roles (insider_cik);

        DROP TABLE IF EXISTS temp.tmp_insider_alias_counts;
        CREATE TEMP TABLE tmp_insider_alias_counts AS
        SELECT insider_cik, COUNT(*) AS distinct_raw_name_count
        FROM """ + qname(alias_table) + """
        WHERE TRIM(raw_name) <> ''
        GROUP BY insider_cik;
        CREATE INDEX idx_tmp_insider_alias_counts_cik ON tmp_insider_alias_counts (insider_cik);
        """
    )
    conn.execute(
        f"""
        INSERT INTO {qname(target)} (
            insider_cik, canonical_name, insider_name_clean, first_seen_date, last_seen_date,
            first_filing_date, last_filing_date, transaction_count, filing_count, company_count,
            purchase_count, sale_count, mvp_transaction_count, is_ever_director,
            is_ever_officer, is_ever_ten_percent_owner, most_recent_role, most_common_role,
            most_common_role_group, distinct_raw_name_count, distinct_role_count,
            name_requires_review, created_at, updated_at
        )
        WITH recent_role AS (
            SELECT insider_cik, normalized_role
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY insider_cik
                    ORDER BY last_role_date DESC, role_count DESC, normalized_role ASC
                ) AS rn
                FROM tmp_insider_roles
            )
            WHERE rn = 1
        ),
        common_role AS (
            SELECT insider_cik, normalized_role, role_group
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY insider_cik
                    ORDER BY role_count DESC, last_role_date DESC, normalized_role ASC
                ) AS rn
                FROM tmp_insider_roles
            )
            WHERE rn = 1
        ),
        distinct_roles AS (
            SELECT insider_cik, COUNT(DISTINCT normalized_role) AS distinct_role_count
            FROM tmp_insider_roles GROUP BY insider_cik
        ),
        canonical AS (
            SELECT insider_cik, raw_name, normalized_name
            FROM {qname(alias_table)}
            WHERE is_canonical = 1
        )
        SELECT a.insider_cik, NULLIF(c.raw_name, ''), NULLIF(c.normalized_name, ''),
               a.first_seen_date, a.last_seen_date, a.first_filing_date, a.last_filing_date,
               a.transaction_count, COALESCE(f.filing_count, 0), COALESCE(co.company_count, 0),
               a.purchase_count, a.sale_count, a.mvp_transaction_count,
               a.is_ever_director, a.is_ever_officer, a.is_ever_ten_percent_owner,
               rr.normalized_role, cr.normalized_role, cr.role_group,
               COALESCE(ac.distinct_raw_name_count, 0),
               COALESCE(dr.distinct_role_count, 0),
               CASE WHEN COALESCE(ac.distinct_raw_name_count, 0) > 1 THEN 1 ELSE 0 END,
               ?, ?
        FROM tmp_insider_agg a
        LEFT JOIN canonical c ON a.insider_cik = c.insider_cik
        LEFT JOIN recent_role rr ON a.insider_cik = rr.insider_cik
        LEFT JOIN common_role cr ON a.insider_cik = cr.insider_cik
        LEFT JOIN tmp_insider_filings f ON a.insider_cik = f.insider_cik
        LEFT JOIN tmp_insider_companies co ON a.insider_cik = co.insider_cik
        LEFT JOIN tmp_insider_alias_counts ac ON a.insider_cik = ac.insider_cik
        LEFT JOIN distinct_roles dr ON a.insider_cik = dr.insider_cik
        """,
        (now, now),
    )
    conn.commit()


def build_company_identifier_history(conn: sqlite3.Connection, target: str) -> None:
    conn.execute(
        f"""
        INSERT INTO {qname(target)} (
            issuer_cik, ticker, company_name, first_seen_date, last_seen_date,
            first_source_year, last_source_year, occurrence_count, is_latest_ticker,
            is_latest_name, ticker_is_valid, ticker_is_multisymbol
        )
        WITH hist AS (
            SELECT issuer_cik,
                   NULLIF(TRIM(ticker_clean), '') AS ticker,
                   NULLIF(TRIM(issuer_name), '') AS company_name,
                   MIN(transaction_date) AS first_seen_date,
                   MAX(transaction_date) AS last_seen_date,
                   MIN(source_year) AS first_source_year,
                   MAX(source_year) AS last_source_year,
                   COUNT(*) AS occurrence_count,
                   MAX(CASE WHEN is_valid_ticker = 1 THEN 1 ELSE 0 END) AS ticker_is_valid,
                   MAX(CASE WHEN is_multisymbol_ticker = 1 THEN 1 ELSE 0 END) AS ticker_is_multisymbol
            FROM tmp_entity_source
            WHERE issuer_cik IS NOT NULL AND TRIM(issuer_cik) <> ''
            GROUP BY issuer_cik, ticker, company_name
        ),
        latest_ticker AS (
            SELECT issuer_cik, ticker FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY issuer_cik
                    ORDER BY last_seen_date DESC, occurrence_count DESC, ticker ASC
                ) AS rn
                FROM hist WHERE ticker_is_valid = 1 AND ticker IS NOT NULL
            ) WHERE rn = 1
        ),
        latest_name AS (
            SELECT issuer_cik, company_name FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY issuer_cik
                    ORDER BY last_seen_date DESC, occurrence_count DESC, company_name ASC
                ) AS rn
                FROM hist WHERE company_name IS NOT NULL
            ) WHERE rn = 1
        )
        SELECT h.issuer_cik, h.ticker, h.company_name, h.first_seen_date, h.last_seen_date,
               h.first_source_year, h.last_source_year, h.occurrence_count,
               CASE WHEN h.ticker = lt.ticker THEN 1 ELSE 0 END,
               CASE WHEN h.company_name = ln.company_name THEN 1 ELSE 0 END,
               h.ticker_is_valid, h.ticker_is_multisymbol
        FROM hist h
        LEFT JOIN latest_ticker lt ON h.issuer_cik = lt.issuer_cik
        LEFT JOIN latest_name ln ON h.issuer_cik = ln.issuer_cik
        """
    )
    conn.commit()


def build_company_dimension(conn: sqlite3.Connection, target: str, history_table: str) -> None:
    now = utc_now()
    conn.executescript(
        """
        DROP TABLE IF EXISTS temp.tmp_company_agg;
        CREATE TEMP TABLE tmp_company_agg AS
        SELECT issuer_cik,
               MIN(transaction_date) AS first_seen_date,
               MAX(transaction_date) AS last_seen_date,
               MIN(filing_date) AS first_filing_date,
               MAX(filing_date) AS last_filing_date,
               COUNT(*) AS transaction_count,
               SUM(CASE WHEN include_mvp_alpha = 1 THEN 1 ELSE 0 END) AS mvp_transaction_count
        FROM tmp_entity_source
        WHERE issuer_cik IS NOT NULL AND TRIM(issuer_cik) <> ''
        GROUP BY issuer_cik;
        CREATE INDEX idx_tmp_company_agg_cik ON tmp_company_agg (issuer_cik);

        DROP TABLE IF EXISTS temp.tmp_company_insiders;
        CREATE TEMP TABLE tmp_company_insiders AS
        SELECT issuer_cik, COUNT(*) AS unique_insider_count
        FROM (
            SELECT DISTINCT issuer_cik, insider_cik
            FROM tmp_entity_source
            WHERE issuer_cik IS NOT NULL AND TRIM(issuer_cik) <> ''
              AND insider_cik IS NOT NULL AND TRIM(insider_cik) <> ''
        )
        GROUP BY issuer_cik;
        CREATE INDEX idx_tmp_company_insiders_cik ON tmp_company_insiders (issuer_cik);
        """
    )
    conn.execute(
        f"""
        INSERT INTO {qname(target)} (
            issuer_cik, canonical_company_name, current_or_latest_ticker,
            first_seen_date, last_seen_date, first_filing_date, last_filing_date,
            transaction_count, mvp_transaction_count, unique_insider_count,
            distinct_name_count, distinct_ticker_count, sector, industry, exchange,
            market_cap, country, currency, metadata_source, metadata_updated_at,
            company_requires_review, created_at, updated_at
        )
        WITH name_counts AS (
            SELECT issuer_cik, COUNT(DISTINCT company_name) AS distinct_name_count
            FROM {qname(history_table)}
            WHERE company_name IS NOT NULL AND TRIM(company_name) <> ''
            GROUP BY issuer_cik
        ),
        ticker_counts AS (
            SELECT issuer_cik, COUNT(DISTINCT ticker) AS distinct_ticker_count
            FROM {qname(history_table)}
            WHERE ticker_is_valid = 1 AND ticker IS NOT NULL
            GROUP BY issuer_cik
        ),
        latest_name AS (
            SELECT issuer_cik, MIN(company_name) AS company_name
            FROM {qname(history_table)}
            WHERE is_latest_name = 1
            GROUP BY issuer_cik
        ),
        latest_ticker AS (
            SELECT issuer_cik, MIN(ticker) AS ticker
            FROM {qname(history_table)}
            WHERE is_latest_ticker = 1
            GROUP BY issuer_cik
        )
        SELECT a.issuer_cik, ln.company_name, lt.ticker,
               a.first_seen_date, a.last_seen_date, a.first_filing_date, a.last_filing_date,
               a.transaction_count, a.mvp_transaction_count, COALESCE(i.unique_insider_count, 0),
               COALESCE(nc.distinct_name_count, 0), COALESCE(tc.distinct_ticker_count, 0),
               NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
               CASE WHEN COALESCE(nc.distinct_name_count, 0) > 1
                      OR COALESCE(tc.distinct_ticker_count, 0) > 1 THEN 1 ELSE 0 END,
               ?, ?
        FROM tmp_company_agg a
        LEFT JOIN tmp_company_insiders i ON a.issuer_cik = i.issuer_cik
        LEFT JOIN name_counts nc ON a.issuer_cik = nc.issuer_cik
        LEFT JOIN ticker_counts tc ON a.issuer_cik = tc.issuer_cik
        LEFT JOIN latest_name ln ON a.issuer_cik = ln.issuer_cik
        LEFT JOIN latest_ticker lt ON a.issuer_cik = lt.issuer_cik
        """,
        (now, now),
    )
    conn.commit()


def build_insider_company_roles(conn: sqlite3.Connection, target: str) -> None:
    conn.execute(
        f"""
        INSERT INTO {qname(target)} (
            insider_cik, issuer_cik, normalized_role, role_group, role_weight,
            is_executive, is_board_member, is_large_owner, raw_role_title,
            raw_relationship, first_seen_date, last_seen_date, filing_count,
            transaction_count, role_requires_review, role_confidence
        )
        SELECT insider_cik, issuer_cik,
               COALESCE(NULLIF(TRIM(normalized_role), ''), 'UNKNOWN'),
               COALESCE(NULLIF(TRIM(role_group), ''), 'UNKNOWN'),
               MAX(role_weight),
               MAX(CASE WHEN is_executive = 1 THEN 1 ELSE 0 END),
               MAX(CASE WHEN is_board_member = 1 THEN 1 ELSE 0 END),
               MAX(CASE WHEN is_large_owner = 1 THEN 1 ELSE 0 END),
               COALESCE(NULLIF(TRIM(raw_role_title), ''), ''),
               COALESCE(NULLIF(TRIM(raw_relationship), ''), ''),
               MIN(transaction_date), MAX(transaction_date),
               COUNT(DISTINCT accession_number), COUNT(*),
               MAX(CASE WHEN role_requires_review = 1 THEN 1 ELSE 0 END),
               MIN(COALESCE(NULLIF(TRIM(role_confidence), ''), 'UNKNOWN'))
        FROM tmp_entity_source
        WHERE insider_cik IS NOT NULL AND TRIM(insider_cik) <> ''
          AND issuer_cik IS NOT NULL AND TRIM(issuer_cik) <> ''
        GROUP BY insider_cik, issuer_cik,
                 COALESCE(NULLIF(TRIM(normalized_role), ''), 'UNKNOWN'),
                 COALESCE(NULLIF(TRIM(role_group), ''), 'UNKNOWN'),
                 COALESCE(NULLIF(TRIM(raw_role_title), ''), ''),
                 COALESCE(NULLIF(TRIM(raw_relationship), ''), '')
        """
    )
    conn.commit()


def create_build_indexes(conn: sqlite3.Connection) -> None:
    index_sql = []
    for table in ENTITY_TABLES:
        build = qname(BUILD_PREFIX + table)
        if table == "clean_insiders":
            index_sql.extend([
                f"CREATE INDEX idx_build_clean_insiders_canonical_name ON {build} (canonical_name)",
                f"CREATE INDEX idx_build_clean_insiders_name_clean ON {build} (insider_name_clean)",
                f"CREATE INDEX idx_build_clean_insiders_most_common_role ON {build} (most_common_role)",
                f"CREATE INDEX idx_build_clean_insiders_last_seen_date ON {build} (last_seen_date)",
            ])
        elif table == "company_dimension":
            index_sql.extend([
                f"CREATE INDEX idx_build_company_dimension_ticker ON {build} (current_or_latest_ticker)",
                f"CREATE INDEX idx_build_company_dimension_name ON {build} (canonical_company_name)",
                f"CREATE INDEX idx_build_company_dimension_last_seen_date ON {build} (last_seen_date)",
            ])
        elif table == "insider_company_roles":
            index_sql.extend([
                f"CREATE INDEX idx_build_roles_insider ON {build} (insider_cik)",
                f"CREATE INDEX idx_build_roles_issuer ON {build} (issuer_cik)",
                f"CREATE INDEX idx_build_roles_role ON {build} (normalized_role)",
                f"CREATE INDEX idx_build_roles_group ON {build} (role_group)",
            ])
        elif table == "company_identifier_history":
            index_sql.extend([
                f"CREATE INDEX idx_build_identifier_issuer ON {build} (issuer_cik)",
                f"CREATE INDEX idx_build_identifier_ticker ON {build} (ticker)",
                f"CREATE INDEX idx_build_identifier_first_seen ON {build} (first_seen_date)",
                f"CREATE INDEX idx_build_identifier_last_seen ON {build} (last_seen_date)",
            ])
    for sql in index_sql:
        conn.execute(sql)
    conn.commit()


def swap_build_tables(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    try:
        for table in ENTITY_TABLES:
            conn.execute(f"DELETE FROM {qname(table)}")
            conn.execute(f"INSERT INTO {qname(table)} SELECT * FROM {qname(BUILD_PREFIX + table)}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def update_metadata(
    conn: sqlite3.Connection,
    build_id: str,
    status: str,
    *,
    completed: bool = False,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE entity_build_metadata
        SET status = ?, completed_at = CASE WHEN ? THEN ? ELSE completed_at END,
            error_message = ?
        WHERE build_id = ?
        """,
        (status, 1 if completed else 0, utc_now(), error, build_id),
    )
    conn.commit()


def export_query(conn: sqlite3.Connection, sql: str, filename: str, export_dir: Path) -> pd.DataFrame:
    df = pd.read_sql_query(sql, conn)
    df.to_csv(export_dir / filename, index=False)
    logger.info("exported %s (%s rows)", filename, len(df))
    return df


def export_qa(conn: sqlite3.Connection, export_dir: Path = EXPORT_DIR) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    export_query(conn, "SELECT * FROM clean_insiders ORDER BY transaction_count DESC", "insider_dimension_summary.csv", export_dir)
    export_query(conn, "SELECT * FROM company_dimension ORDER BY transaction_count DESC", "company_dimension_summary.csv", export_dir)
    export_query(conn, "SELECT * FROM insider_company_roles ORDER BY transaction_count DESC", "role_relationship_summary.csv", export_dir)
    export_query(conn, "SELECT * FROM company_identifier_history ORDER BY issuer_cik, first_seen_date", "company_ticker_history.csv", export_dir)
    export_query(conn, """
        SELECT i.insider_cik, i.canonical_name, i.distinct_raw_name_count,
               GROUP_CONCAT(a.raw_name, ' | ') AS raw_names,
               i.transaction_count, i.first_seen_date, i.last_seen_date
        FROM clean_insiders i JOIN insider_name_aliases a ON i.insider_cik = a.insider_cik
        WHERE i.distinct_raw_name_count > 1
        GROUP BY i.insider_cik
        ORDER BY i.distinct_raw_name_count DESC, i.transaction_count DESC
        """, "insider_name_variants.csv", export_dir)
    export_query(conn, """
        SELECT normalized_name, COUNT(DISTINCT insider_cik) AS cik_count,
               GROUP_CONCAT(DISTINCT insider_cik) AS insider_ciks,
               GROUP_CONCAT(DISTINCT raw_name) AS raw_names,
               SUM(occurrence_count) AS occurrence_count
        FROM insider_name_aliases
        WHERE normalized_name IS NOT NULL AND TRIM(normalized_name) <> ''
        GROUP BY normalized_name
        HAVING COUNT(DISTINCT insider_cik) > 1
        ORDER BY cik_count DESC, occurrence_count DESC
        """, "insider_same_name_different_cik.csv", export_dir)
    export_query(conn, """
        SELECT issuer_cik, canonical_company_name, distinct_name_count,
               transaction_count, first_seen_date, last_seen_date
        FROM company_dimension
        WHERE distinct_name_count > 1
        ORDER BY distinct_name_count DESC, transaction_count DESC
        """, "company_name_variants.csv", export_dir)
    export_query(conn, """
        SELECT issuer_cik, canonical_company_name, distinct_ticker_count,
               current_or_latest_ticker, transaction_count
        FROM company_dimension
        WHERE distinct_ticker_count > 1
        ORDER BY distinct_ticker_count DESC, transaction_count DESC
        """, "companies_multiple_tickers.csv", export_dir)
    export_query(conn, """
        SELECT ticker, COUNT(DISTINCT issuer_cik) AS issuer_cik_count,
               GROUP_CONCAT(DISTINCT issuer_cik) AS issuer_ciks,
               MIN(first_seen_date) AS first_seen_date,
               MAX(last_seen_date) AS last_seen_date,
               SUM(occurrence_count) AS occurrence_count
        FROM company_identifier_history
        WHERE ticker_is_valid = 1 AND ticker IS NOT NULL
        GROUP BY ticker
        HAVING COUNT(DISTINCT issuer_cik) > 1
        ORDER BY issuer_cik_count DESC, occurrence_count DESC
        """, "tickers_multiple_ciks.csv", export_dir)
    export_query(conn, """
        WITH totals AS (
            SELECT COUNT(*) AS clean_rows,
                   SUM(CASE WHEN include_mvp_alpha = 1 THEN 1 ELSE 0 END) AS mvp_rows
            FROM clean_insider_transactions
        ),
        linked AS (
            SELECT SUM(CASE WHEN ci.insider_cik IS NOT NULL THEN 1 ELSE 0 END) AS linked_insiders,
                   SUM(CASE WHEN cd.issuer_cik IS NOT NULL THEN 1 ELSE 0 END) AS linked_companies,
                   SUM(CASE WHEN ci.insider_cik IS NOT NULL AND cd.issuer_cik IS NOT NULL THEN 1 ELSE 0 END) AS linked_both,
                   SUM(CASE WHEN t.include_mvp_alpha = 1 AND ci.insider_cik IS NOT NULL AND cd.issuer_cik IS NOT NULL THEN 1 ELSE 0 END) AS mvp_linked_both
            FROM clean_insider_transactions t
            LEFT JOIN clean_insiders ci ON t.insider_cik = ci.insider_cik
            LEFT JOIN company_dimension cd ON t.issuer_cik = cd.issuer_cik
        )
        SELECT 'clean_transactions_linked_to_insiders_pct' AS metric,
               ROUND(100.0 * linked_insiders / NULLIF(clean_rows, 0), 4) AS value FROM totals, linked
        UNION ALL SELECT 'clean_transactions_linked_to_companies_pct',
               ROUND(100.0 * linked_companies / NULLIF(clean_rows, 0), 4) FROM totals, linked
        UNION ALL SELECT 'clean_transactions_linked_to_both_pct',
               ROUND(100.0 * linked_both / NULLIF(clean_rows, 0), 4) FROM totals, linked
        UNION ALL SELECT 'mvp_transactions_linked_to_both_pct',
               ROUND(100.0 * mvp_linked_both / NULLIF(mvp_rows, 0), 4) FROM totals, linked
        """, "entity_linkage_coverage.csv", export_dir)
    export_query(conn, """
        SELECT 'insider_name' AS candidate_type, insider_cik AS entity_key,
               canonical_name AS label, distinct_raw_name_count AS review_metric
        FROM clean_insiders WHERE name_requires_review = 1
        UNION ALL
        SELECT 'company_identifier', issuer_cik, canonical_company_name,
               distinct_name_count + distinct_ticker_count
        FROM company_dimension WHERE company_requires_review = 1
        UNION ALL
        SELECT 'role', insider_cik || ':' || issuer_cik, normalized_role, role_requires_review
        FROM insider_company_roles WHERE role_requires_review = 1
        """, "entity_review_candidates.csv", export_dir)


def build_entity_dimensions(
    conn: sqlite3.Connection,
    *,
    reset: bool = False,
    export_dir: Path = EXPORT_DIR,
    limit: int | None = None,
    export: bool = True,
) -> str:
    if not table_exists(conn, "clean_insider_transactions"):
        raise RuntimeError("clean_insider_transactions not found. Run Phase 3 first.")
    ensure_entity_schema(conn)
    build_id = uuid.uuid4().hex
    source_rows = scalar(conn, "SELECT COUNT(*) FROM clean_insider_transactions")
    source_max_rowid = scalar(conn, "SELECT MAX(rowid) FROM clean_insider_transactions")
    status = "SAMPLE" if limit else "IN_PROGRESS"
    conn.execute(
        """
        INSERT INTO entity_build_metadata (
            build_id, started_at, completed_at, status, source_clean_row_count,
            source_max_rowid, builder_version, is_sample, sample_limit, error_message
        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (build_id, utc_now(), status, source_rows, source_max_rowid, BUILDER_VERSION, 1 if limit else 0, limit),
    )
    conn.commit()
    total_timer = StepTimer()
    try:
        logger.info("Phase 4 entity build build_id=%s limit=%s", build_id, limit)
        drop_build_tables(conn)
        create_build_tables(conn)

        timer = log_step(1, 7, "Staging source rows")
        create_temp_source(conn, limit)
        staged_rows = scalar(conn, "SELECT COUNT(*) FROM tmp_entity_source")
        mvp_rows = scalar(conn, "SELECT SUM(include_mvp_alpha) FROM tmp_entity_source")
        log_done("source staging", timer, staged_rows)

        timer = log_step(2, 7, "Insider name normalization and aliases")
        create_name_normalization_map(conn)
        build_insider_aliases(conn, BUILD_PREFIX + "insider_name_aliases")
        log_done("insider aliases", timer, scalar(conn, f"SELECT COUNT(*) FROM {qname(BUILD_PREFIX + 'insider_name_aliases')}"))

        timer = log_step(3, 7, "Insider dimension")
        build_clean_insiders(conn, BUILD_PREFIX + "clean_insiders", BUILD_PREFIX + "insider_name_aliases")
        log_done("clean insiders", timer, scalar(conn, f"SELECT COUNT(*) FROM {qname(BUILD_PREFIX + 'clean_insiders')}"))

        timer = log_step(4, 7, "Company identifier history")
        build_company_identifier_history(conn, BUILD_PREFIX + "company_identifier_history")
        log_done("identifier history", timer, scalar(conn, f"SELECT COUNT(*) FROM {qname(BUILD_PREFIX + 'company_identifier_history')}"))

        timer = log_step(5, 7, "Company dimension")
        build_company_dimension(conn, BUILD_PREFIX + "company_dimension", BUILD_PREFIX + "company_identifier_history")
        log_done("company dimension", timer, scalar(conn, f"SELECT COUNT(*) FROM {qname(BUILD_PREFIX + 'company_dimension')}"))

        timer = log_step(6, 7, "Insider-company roles")
        build_insider_company_roles(conn, BUILD_PREFIX + "insider_company_roles")
        log_done("role relationships", timer, scalar(conn, f"SELECT COUNT(*) FROM {qname(BUILD_PREFIX + 'insider_company_roles')}"))

        timer = log_step(7, 7, "Swapping staged tables")
        create_build_indexes(conn)
        swap_build_tables(conn)
        log_done("table swap", timer)

        final_status = "SAMPLE" if limit else "COMPLETE"
        update_metadata(conn, build_id, final_status, completed=True)
        if export:
            export_qa(conn, export_dir)
        logger.info("Phase 4 entity build complete in %.2fs", total_timer.elapsed())
        logger.info("source clean rows=%s staged rows=%s mvp rows=%s", source_rows, staged_rows, mvp_rows)
        for table in ENTITY_TABLES:
            logger.info("%s rows=%s", table, scalar(conn, f"SELECT COUNT(*) FROM {qname(table)}"))
        return build_id
    except Exception as exc:
        update_metadata(conn, build_id, "FAILED", completed=True, error=str(exc))
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Phase 4 entity dimensions.")
    parser.add_argument("--reset", action="store_true", help="Rebuild entity tables.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--chunk-size", type=int, default=100_000, help="Reserved for compatibility.")
    parser.add_argument("--limit", type=int, default=None, help="Build a non-production sample from the first N clean rows.")
    parser.add_argument("--no-export", action="store_true", help="Skip QA CSV exports.")
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
        build_entity_dimensions(conn, reset=args.reset, limit=args.limit, export=not args.no_export)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
