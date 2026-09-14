"""
Build clean_insider_transactions from raw SEC tables (chunked, non-destructive).

Usage:
    python -m src.cleaning.build_clean_insider_transactions --reset
    python -m src.cleaning.build_clean_insider_transactions --reset --limit 100000
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
import yaml

from src.cleaning.clean_logic import (
    CLEAN_COLUMNS,
    load_cleaning_config,
    transform_row,
)
from src.analytics.unknown_role_refinement import (
    analyze_unknown_roles_for_refinement,
    export_pre_source_transaction_date_sample,
)
from src.utils.db import PROJECT_ROOT, execute_sql_file, get_connection

VALIDATION_EXPORT_DIR = PROJECT_ROOT / "data" / "exports" / "validation"

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "insider_research.db"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning.yaml"
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "sql" / "002_create_clean_tables.sql"
EXPORT_DIR = PROJECT_ROOT / "data" / "exports" / "cleaning"

JOIN_QUERY = """
SELECT
    t.ACCESSION_NUMBER,
    t.NONDERIV_TRANS_SK,
    t.TRANS_DATE,
    t.TRANS_CODE,
    t.TRANS_SHARES,
    t.TRANS_PRICEPERSHARE,
    t.SHRS_OWND_FOLWNG_TRANS,
    t.SECURITY_TITLE,
    t.TRANS_ACQUIRED_DISP_CD,
    t.DIRECT_INDIRECT_OWNERSHIP,
    t.NATURE_OF_OWNERSHIP,
    t.source_year,
    t.source_quarter,
    t.source_file,
    t.source_folder,
    s.FILING_DATE,
    s.PERIOD_OF_REPORT,
    s.DOCUMENT_TYPE,
    s.ISSUERCIK,
    s.ISSUERNAME,
    s.ISSUERTRADINGSYMBOL,
    o.RPTOWNERCIK,
    o.RPTOWNERNAME,
    o.RPTOWNER_RELATIONSHIP,
    o.RPTOWNER_TITLE
FROM raw_nonderiv_trans t
INNER JOIN raw_submission s
    ON t.ACCESSION_NUMBER = s.ACCESSION_NUMBER
LEFT JOIN raw_reporting_owner o
    ON t.ACCESSION_NUMBER = o.ACCESSION_NUMBER
WHERE t.rowid > ? AND t.rowid <= ?
"""


def load_yaml_config(config_path: Path) -> dict[str, Any]:
    with config_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return load_cleaning_config(data.get("cleaning", data))


def ensure_clean_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    execute_sql_file(conn, schema_path)


def drop_clean_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS clean_insider_transactions")
    conn.commit()


def iter_rowid_chunks(
    conn: sqlite3.Connection,
    chunk_size: int,
) -> Iterator[tuple[int, int]]:
    row = conn.execute(
        "SELECT MIN(rowid), MAX(rowid) FROM raw_nonderiv_trans"
    ).fetchone()
    if row is None or row[0] is None:
        return
    min_rowid, max_rowid = int(row[0]), int(row[1])
    cursor = min_rowid
    while cursor <= max_rowid:
        end = min(cursor + chunk_size - 1, max_rowid)
        yield cursor, end
        cursor = end + 1


def insert_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    columns_sql = ", ".join(CLEAN_COLUMNS)
    placeholders = ", ".join("?" for _ in CLEAN_COLUMNS)
    sql = (
        f"INSERT OR REPLACE INTO clean_insider_transactions ({columns_sql}) "
        f"VALUES ({placeholders})"
    )
    rows = [tuple(record[col] for col in CLEAN_COLUMNS) for record in records]
    conn.executemany(sql, rows)
    conn.commit()


def process_chunk(
    df: pd.DataFrame,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        row_dict = row.to_dict()
        records.append(transform_row(row_dict, config, row_index=int(idx)))
    return records


def build_clean_table(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    *,
    chunk_size: int = 50_000,
    limit: int | None = None,
    verbose: bool = False,
) -> int:
    total_inserted = 0
    chunk_num = 0

    for start_rowid, end_rowid in iter_rowid_chunks(conn, chunk_size):
        if limit is not None and total_inserted >= limit:
            break

        chunk_num += 1
        df = pd.read_sql_query(
            JOIN_QUERY,
            conn,
            params=(start_rowid - 1, end_rowid),
        )
        if df.empty:
            continue

        if limit is not None:
            remaining = limit - total_inserted
            if len(df) > remaining:
                df = df.head(remaining)

        records = process_chunk(df, config)
        insert_records(conn, records)
        total_inserted += len(records)

        if verbose or chunk_num % 10 == 0:
            logger.info(
                "Chunk %s: rowid %s-%s -> %s rows (total %s)",
                chunk_num,
                start_rowid,
                end_rowid,
                len(records),
                total_inserted,
            )

    return total_inserted


def export_summaries(conn: sqlite3.Connection, export_dir: Path) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)

    summary_sql = """
    SELECT
        COUNT(*) AS total_clean_rows,
        SUM(include_mvp_alpha) AS mvp_alpha_rows,
        SUM(CASE WHEN include_mvp_alpha = 0 THEN 1 ELSE 0 END) AS non_mvp_rows,
        SUM(is_form_3) AS form_3_rows,
        SUM(is_form_4) AS form_4_rows,
        SUM(is_form_5) AS form_5_rows,
        SUM(is_amendment) AS amendment_rows,
        SUM(CASE WHEN transaction_code = 'P' THEN 1 ELSE 0 END) AS p_rows,
        SUM(CASE WHEN transaction_code = 'S' THEN 1 ELSE 0 END) AS s_rows,
        SUM(CASE WHEN normalized_security_type = 'COMMON_EQUITY' THEN 1 ELSE 0 END)
            AS common_equity_rows,
        SUM(is_valid_clean_event) AS valid_clean_event_rows,
        SUM(CASE WHEN is_valid_clean_event = 0 THEN 1 ELSE 0 END) AS invalid_event_rows,
        SUM(is_missing_price) AS missing_price_rows,
        SUM(is_zero_price) AS zero_price_rows,
        SUM(is_negative_or_zero_shares) AS negative_or_zero_shares_rows,
        SUM(is_filing_before_transaction) AS filing_before_transaction_rows,
        SUM(is_tiny_transaction) AS tiny_transaction_rows,
        SUM(is_extreme_transaction_value) AS extreme_transaction_value_rows,
        SUM(is_invalid_ticker_placeholder) AS invalid_ticker_placeholder_rows,
        SUM(is_multisymbol_ticker) AS multisymbol_ticker_rows,
        SUM(is_stale_filing) AS stale_filing_rows,
        SUM(is_pre_source_transaction_date) AS pre_source_transaction_date_rows,
        SUM(CASE WHEN include_mvp_alpha = 1 AND is_invalid_ticker_placeholder = 1 THEN 1 ELSE 0 END)
            AS mvp_invalid_ticker_rows
    FROM clean_insider_transactions
    """
    summary_df = pd.read_sql_query(summary_sql, conn)
    summary_df.to_csv(
        export_dir / "clean_insider_transactions_summary.csv",
        index=False,
    )

    flag_columns = [
        "is_missing_price",
        "is_zero_price",
        "is_negative_or_zero_shares",
        "is_missing_transaction_date",
        "is_missing_filing_date",
        "is_filing_before_transaction",
        "is_extreme_transaction_value",
        "is_tiny_transaction",
        "is_valid_clean_event",
        "include_mvp_alpha",
        "is_valid_ticker",
        "is_invalid_ticker_placeholder",
        "is_multisymbol_ticker",
        "is_stale_filing",
        "is_pre_source_transaction_date",
    ]
    flag_rows: list[dict[str, Any]] = []
    for flag in flag_columns:
        count = conn.execute(
            f"SELECT SUM({flag}) FROM clean_insider_transactions"
        ).fetchone()[0]
        flag_rows.append({"quality_flag": flag, "row_count": count or 0})
    pd.DataFrame(flag_rows).to_csv(
        export_dir / "clean_quality_flags_summary.csv",
        index=False,
    )

    preview_sql = """
    SELECT *
    FROM clean_insider_transactions
    WHERE include_mvp_alpha = 1
    ORDER BY transaction_value DESC
    LIMIT 1000
    """
    pd.read_sql_query(preview_sql, conn).to_csv(
        export_dir / "mvp_alpha_universe_preview.csv",
        index=False,
    )
    logger.info("Exported cleaning summaries to %s", export_dir)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build clean_insider_transactions from raw SEC data.",
    )
    parser.add_argument("--reset", action="store_true", help="Drop and rebuild clean table.")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite database path.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Cleaning YAML config path.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=50_000,
        help="Chunk size on raw_nonderiv_trans rowid ranges.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max clean rows to insert (for testing).",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose progress logging.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_yaml_config(args.config)
    conn = get_connection(args.db_path)

    try:
        if args.reset:
            logger.info("Resetting clean_insider_transactions")
            drop_clean_table(conn)
        ensure_clean_schema(conn, DEFAULT_SCHEMA_PATH)

        logger.info("Building clean layer (chunk_size=%s, limit=%s)", args.chunk_size, args.limit)
        total = build_clean_table(
            conn,
            config,
            chunk_size=args.chunk_size,
            limit=args.limit,
            verbose=args.verbose,
        )
        logger.info("Inserted %s clean rows", total)

        export_summaries(conn, EXPORT_DIR)
        export_pre_source_transaction_date_sample(conn, EXPORT_DIR)
        analyze_unknown_roles_for_refinement(conn, VALIDATION_EXPORT_DIR)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
