"""
Debug inspector for raw SEC SQLite tables.

Exports schema samples and value patterns to data/exports/raw_data_audit/debug_raw_fields/.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.db import PROJECT_ROOT, get_connection, get_database_path, get_table_columns
from src.utils.sec_parsing import parse_sec_date, parse_sec_number

logger = logging.getLogger(__name__)

TABLES = ("raw_submission", "raw_nonderiv_trans", "raw_reporting_owner")

DATE_CANDIDATES = {
    "raw_submission": ["FILING_DATE", "PERIOD_OF_REPORT", "DATE_OF_ORIG_SUB"],
    "raw_nonderiv_trans": ["TRANS_DATE", "DEEMED_EXECUTION_DATE"],
    "raw_reporting_owner": [],
}

NUMERIC_CANDIDATES = {
    "raw_submission": [],
    "raw_nonderiv_trans": [
        "TRANS_SHARES",
        "TRANS_PRICEPERSHARE",
        "SHRS_OWND_FOLWNG_TRANS",
        "VALU_OWND_FOLWNG_TRANS",
    ],
    "raw_reporting_owner": [],
}

KEY_CANDIDATES = {
    "raw_submission": [
        "ACCESSION_NUMBER",
        "FILING_DATE",
        "PERIOD_OF_REPORT",
        "DOCUMENT_TYPE",
        "ISSUERTRADINGSYMBOL",
        "ISSUERCIK",
    ],
    "raw_nonderiv_trans": [
        "ACCESSION_NUMBER",
        "TRANS_DATE",
        "TRANS_CODE",
        "TRANS_SHARES",
        "TRANS_PRICEPERSHARE",
        "SECURITY_TITLE",
    ],
    "raw_reporting_owner": [
        "ACCESSION_NUMBER",
        "RPTOWNERCIK",
        "RPTOWNERNAME",
        "RPTOWNER_RELATIONSHIP",
        "RPTOWNER_TITLE",
    ],
}

DEFAULT_OUT = PROJECT_ROOT / "data" / "exports" / "raw_data_audit" / "debug_raw_fields"


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def inspect_table_schema(conn: sqlite3.Connection, table_name: str) -> pd.DataFrame:
    columns = get_table_columns(conn, table_name)
    return pd.DataFrame({"table_name": table_name, "column_name": columns})


def sample_column_values(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    limit: int = 20,
) -> pd.DataFrame:
    query = f"""
        SELECT TRIM({column_name}) AS sample_value
        FROM {table_name}
        WHERE {column_name} IS NOT NULL AND TRIM({column_name}) != ''
        LIMIT {limit}
    """
    return pd.read_sql_query(query, conn)


def count_non_null(conn: sqlite3.Connection, table_name: str, column_name: str) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM {table_name}
        WHERE {column_name} IS NOT NULL AND TRIM({column_name}) != ''
        """
    ).fetchone()
    return int(row[0])


def inspect_date_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    limit: int = 20,
) -> dict[str, Any]:
    samples = sample_column_values(conn, table_name, column_name, limit)
    parsed = [parse_sec_date(v) for v in samples["sample_value"].tolist()]
    return {
        "table_name": table_name,
        "column_name": column_name,
        "field_type": "date",
        "non_null_count": count_non_null(conn, table_name, column_name),
        "sample_values": samples["sample_value"].tolist(),
        "parsed_iso_samples": [d.isoformat() if d else None for d in parsed],
        "parse_success_in_sample": sum(1 for d in parsed if d is not None),
    }


def inspect_numeric_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    limit: int = 20,
) -> dict[str, Any]:
    samples = sample_column_values(conn, table_name, column_name, limit)
    parsed = [parse_sec_number(v) for v in samples["sample_value"].tolist()]
    return {
        "table_name": table_name,
        "column_name": column_name,
        "field_type": "numeric",
        "non_null_count": count_non_null(conn, table_name, column_name),
        "sample_values": samples["sample_value"].tolist(),
        "parsed_numeric_samples": parsed,
        "parse_success_in_sample": sum(1 for v in parsed if v is not None),
    }


def run_debug_inspection(
    conn: sqlite3.Connection,
    out_dir: Path | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    out = out_dir or DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)

    schema_frames: list[pd.DataFrame] = []
    inspection_rows: list[dict[str, Any]] = []

    for table_name in TABLES:
        if table_name not in get_table_columns(conn, "sqlite_master"):
            pass
        schema_frames.append(inspect_table_schema(conn, table_name))
        actual_columns = set(get_table_columns(conn, table_name))

        for column in KEY_CANDIDATES.get(table_name, []):
            if column not in actual_columns:
                inspection_rows.append(
                    {
                        "table_name": table_name,
                        "column_name": column,
                        "field_type": "missing_column",
                        "non_null_count": 0,
                        "note": "expected column not in table",
                    }
                )
                continue
            if column in DATE_CANDIDATES.get(table_name, []) or column.endswith("_DATE"):
                inspection_rows.append(
                    inspect_date_column(conn, table_name, column, sample_limit)
                )
            elif column in NUMERIC_CANDIDATES.get(table_name, []):
                inspection_rows.append(
                    inspect_numeric_column(conn, table_name, column, sample_limit)
                )
            else:
                row = inspect_numeric_column(conn, table_name, column, sample_limit)
                row["field_type"] = "text"
                inspection_rows.append(row)

        for column in DATE_CANDIDATES.get(table_name, []):
            if column in actual_columns and column not in KEY_CANDIDATES.get(table_name, []):
                inspection_rows.append(
                    inspect_date_column(conn, table_name, column, sample_limit)
                )

        for column in NUMERIC_CANDIDATES.get(table_name, []):
            if column in actual_columns and column not in KEY_CANDIDATES.get(table_name, []):
                inspection_rows.append(
                    inspect_numeric_column(conn, table_name, column, sample_limit)
                )

    schema_df = pd.concat(schema_frames, ignore_index=True)
    schema_df.to_csv(out / "table_schemas.csv", index=False)

    summary_df = pd.DataFrame(inspection_rows)
    summary_df.to_csv(out / "field_inspection_summary.csv", index=False)

    (out / "field_inspection_detail.json").write_text(
        json.dumps(inspection_rows, indent=2, default=str),
        encoding="utf-8",
    )

    print(f"\n=== Debug inspection: {out} ===\n")
    for table_name in TABLES:
        print(f"{table_name} columns ({len(get_table_columns(conn, table_name))}):")
        print(", ".join(get_table_columns(conn, table_name)))
        print()

    for row in inspection_rows:
        if row.get("field_type") == "date":
            print(
                f"{row['table_name']}.{row['column_name']}: "
                f"non_null={row['non_null_count']:,} "
                f"sample_parse={row.get('parse_success_in_sample')}/{sample_limit}"
            )
            print(f"  samples: {row.get('sample_values', [])[:5]}")
        elif row.get("field_type") == "missing_column":
            print(f"{row['table_name']}.{row['column_name']}: MISSING")

    logger.info("wrote debug exports to %s", out)
    return {"schema": schema_df, "inspection": summary_df, "details": inspection_rows}


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect raw SEC SQLite field formats.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    configure_logging(args.verbose)

    db_path = get_database_path()
    print(f"Database: {db_path}")
    conn = get_connection(db_path)
    try:
        run_debug_inspection(conn, args.out_dir)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
