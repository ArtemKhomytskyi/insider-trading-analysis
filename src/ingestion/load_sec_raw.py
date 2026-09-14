"""
Legacy SEC raw loader (single-pass, ingestion_log dedup).

Prefer Phase 1.1 loader:
    python -m src.ingestion.load_all_sec_datasets
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.utils.db import (
    PROJECT_ROOT,
    get_connection,
    get_table_columns,
    is_file_ingested,
    log_ingestion,
    row_count,
)

RAW_SEC_DIR = PROJECT_ROOT / "data" / "raw" / "sec"

FILE_TO_TABLE: dict[str, str] = {
    "SUBMISSION.tsv": "raw_submission",
    "REPORTINGOWNER.tsv": "raw_reporting_owner",
    "NONDERIV_TRANS.tsv": "raw_nonderiv_trans",
    "NONDERIV_HOLDING.tsv": "raw_nonderiv_holding",
    "DERIV_TRANS.tsv": "raw_deriv_trans",
    "DERIV_HOLDING.tsv": "raw_deriv_holding",
    "FOOTNOTES.tsv": "raw_footnotes",
    "OWNER_SIGNATURE.tsv": "raw_owner_signature",
}

METADATA_COLUMNS = (
    "source_file",
    "source_year",
    "source_quarter",
    "source_folder",
    "ingested_at",
    "row_hash",
)
QUARTER_PATTERN = re.compile(r"(?P<year>\d{4})q(?P<quarter>[1-4])", re.IGNORECASE)


def parse_year_quarter(path: Path) -> tuple[int | None, int | None]:
    for part in path.parts:
        match = QUARTER_PATTERN.search(part)
        if match:
            return int(match.group("year")), int(match.group("quarter"))
    return None, None


def relative_source_key(path: Path, raw_root: Path) -> str:
    """Build a stable source key, including quarter folder when TSVs sit at raw_root."""
    resolved_path = path.resolve()
    resolved_root = raw_root.resolve()
    try:
        rel = resolved_path.relative_to(resolved_root)
    except ValueError:
        return resolved_path.as_posix()

    if len(rel.parts) == 1 and resolved_root.name:
        return f"{resolved_root.name}/{rel.as_posix()}"
    return rel.as_posix()


def align_dataframe_to_table(
    df: pd.DataFrame,
    table_columns: list[str],
) -> pd.DataFrame:
    """Match dataframe columns to table schema; fill missing columns with empty strings."""
    data_columns = [c for c in table_columns if c not in METADATA_COLUMNS]
    aligned = pd.DataFrame(index=df.index)

    for column in data_columns:
        if column in df.columns:
            aligned[column] = df[column].astype(str)
        else:
            aligned[column] = ""

    for column in df.columns:
        if column not in data_columns and column not in METADATA_COLUMNS:
            print(f"  warning: ignoring unexpected column '{column}'")

    return aligned


def load_tsv_file(
    tsv_path: Path,
    table_name: str,
    raw_root: Path,
    conn,
    force: bool = False,
) -> int:
    source_key = relative_source_key(tsv_path, raw_root)

    if is_file_ingested(conn, source_key, table_name):
        if not force:
            print(f"skip (already ingested): {source_key} -> {table_name}")
            return 0
        conn.execute(
            f"DELETE FROM {table_name} WHERE source_file = ?",
            (source_key,),
        )
        conn.execute(
            """
            DELETE FROM ingestion_log
            WHERE source_file = ? AND target_table = ?
            """,
            (source_key, table_name),
        )
        conn.commit()
        print(f"force reload: removed prior rows for {source_key}")

    source_year, source_quarter = parse_year_quarter(tsv_path)
    ingested_at = datetime.now(timezone.utc).isoformat()

    df = pd.read_csv(
        tsv_path,
        sep="\t",
        dtype=str,
        keep_default_na=False,
        na_filter=False,
        low_memory=False,
    )
    df.columns = [str(c).strip() for c in df.columns]

    table_columns = get_table_columns(conn, table_name)
    aligned = align_dataframe_to_table(df, table_columns)

    aligned["source_file"] = source_key
    aligned["source_year"] = source_year
    aligned["source_quarter"] = source_quarter
    aligned["ingested_at"] = ingested_at

    insert_columns = [c for c in table_columns if c in aligned.columns]
    placeholders = ", ".join("?" for _ in insert_columns)
    column_list = ", ".join(insert_columns)
    insert_sql = f"INSERT INTO {table_name} ({column_list}) VALUES ({placeholders})"

    # SQLite allows at most 999 bound variables per statement.
    max_sqlite_variables = 999
    batch_size = max(1, max_sqlite_variables // len(insert_columns))
    records = [
        tuple(row[col] for col in insert_columns)
        for row in aligned[insert_columns].to_dict(orient="records")
    ]
    for start in range(0, len(records), batch_size):
        conn.executemany(insert_sql, records[start : start + batch_size])

    loaded_rows = len(aligned)
    log_ingestion(
        conn,
        source_file=source_key,
        target_table=table_name,
        row_count_value=loaded_rows,
        source_year=source_year,
        source_quarter=source_quarter,
        ingested_at=ingested_at,
    )
    conn.commit()

    print(f"loaded {loaded_rows:,} rows: {source_key} -> {table_name}")
    return loaded_rows


def discover_tsv_files(raw_root: Path) -> list[Path]:
    files: list[Path] = []
    for name in FILE_TO_TABLE:
        files.extend(sorted(raw_root.rglob(name)))
    return files


def print_table_counts(conn) -> None:
    print("\n--- raw table row counts ---")
    for table_name in sorted(set(FILE_TO_TABLE.values())):
        count = row_count(conn, table_name)
        print(f"  {table_name}: {count:,}")


def run_ingestion(raw_dir: Path | None = None, force: bool = False) -> None:
    raw_root = raw_dir or RAW_SEC_DIR
    if not raw_root.exists():
        print(f"raw directory not found: {raw_root}")
        print("Place SEC quarterly TSV folders under data/raw/sec/")
        sys.exit(1)

    tsv_files = discover_tsv_files(raw_root)
    if not tsv_files:
        print(f"no SEC TSV files found under {raw_root}")
        sys.exit(1)

    conn = get_connection()
    total_loaded = 0

    try:
        for tsv_path in tsv_files:
            table_name = FILE_TO_TABLE[tsv_path.name]
            total_loaded += load_tsv_file(
                tsv_path, table_name, raw_root, conn, force=force
            )
        print_table_counts(conn)
        print(f"\ntotal rows loaded this run: {total_loaded:,}")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Load raw SEC TSV files into SQLite.")
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=RAW_SEC_DIR,
        help="Directory containing SEC TSV files (searched recursively).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reload files even if already present in ingestion_log.",
    )
    args = parser.parse_args()
    run_ingestion(raw_dir=args.raw_dir, force=args.force)


if __name__ == "__main__":
    main()
