"""
Phase 1.1: ingest all SEC Form 3/4/5 quarterly TSV datasets into SQLite raw tables.

Scans data/raw/sec/ recursively, loads known TSV files in chunks, and deduplicates
rows using a stable row_hash (original values + source_file + source_folder).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.ingestion.sec_ingestion import (
    FILE_TO_TABLE,
    KNOWN_SEC_FILES,
    RAW_TABLES,
    IngestionSummary,
    add_row_hashes,
    align_chunk_to_table,
    audit_missing_files,
    is_known_sec_file,
    parse_year_quarter_from_path,
    relative_to_raw,
)
from src.utils.db import (
    PROJECT_ROOT,
    ensure_raw_schema,
    get_connection,
    get_table_columns,
)

logger = logging.getLogger(__name__)

DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw" / "sec"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "insider_research.db"
DEFAULT_CHUNK_SIZE = 50_000
SQLITE_MAX_VARIABLES = 999


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def discover_source_folders(raw_root: Path) -> list[Path]:
    folders: set[Path] = set()
    for filename in KNOWN_SEC_FILES:
        for tsv_path in raw_root.rglob(filename):
            folders.add(tsv_path.parent.resolve())
    return sorted(folders, key=lambda folder: folder.as_posix().lower())


def insert_records(
    conn,
    table_name: str,
    insert_columns: list[str],
    records: list[tuple],
) -> tuple[int, int]:
    if not records:
        return 0, 0

    placeholders = ", ".join("?" for _ in insert_columns)
    column_list = ", ".join(insert_columns)
    insert_sql = (
        f"INSERT OR IGNORE INTO {table_name} ({column_list}) "
        f"VALUES ({placeholders})"
    )

    batch_size = max(1, SQLITE_MAX_VARIABLES // len(insert_columns))
    inserted = 0

    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        changes_before = conn.total_changes
        conn.executemany(insert_sql, batch)
        inserted += conn.total_changes - changes_before

    skipped = len(records) - inserted
    return inserted, skipped


def load_tsv_file(
    tsv_path: Path,
    table_name: str,
    raw_root: Path,
    conn,
    summary: IngestionSummary,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> None:
    source_file = relative_to_raw(tsv_path, raw_root)
    source_folder = tsv_path.parent.name
    source_year, source_quarter = parse_year_quarter_from_path(tsv_path)
    ingested_at = datetime.now(timezone.utc).isoformat()

    table_columns = get_table_columns(conn, table_name)
    insert_columns = table_columns

    stats = summary.get_table_stats(table_name)
    logger.info("loading %s -> %s", source_file, table_name)

    try:
        reader = pd.read_csv(
            tsv_path,
            sep="\t",
            dtype=str,
            keep_default_na=False,
            na_filter=False,
            chunksize=chunk_size,
            encoding="utf-8",
            encoding_errors="replace",
            on_bad_lines="warn",
            low_memory=False,
        )
    except Exception as exc:
        logger.error("failed to read %s: %s", tsv_path, exc)
        raise

    for chunk_number, chunk in enumerate(reader, start=1):
        chunk.columns = [str(column).strip() for column in chunk.columns]
        aligned, data_columns = align_chunk_to_table(chunk, table_columns)

        aligned["source_file"] = source_file
        aligned["source_year"] = source_year
        aligned["source_quarter"] = source_quarter
        aligned["source_folder"] = source_folder
        aligned["ingested_at"] = ingested_at
        aligned["row_hash"] = add_row_hashes(
            aligned,
            data_columns,
            source_file,
            source_folder,
        )

        records = [
            tuple(row[column] for column in insert_columns)
            for row in aligned[insert_columns].to_dict(orient="records")
        ]
        inserted, skipped = insert_records(conn, table_name, insert_columns, records)
        stats.inserted += inserted
        stats.skipped += skipped
        conn.commit()

        logger.debug(
            "%s chunk %s: inserted=%s skipped=%s",
            source_file,
            chunk_number,
            inserted,
            skipped,
        )

    if source_year is not None:
        summary.years_detected.append(source_year)


def collect_tsv_files(raw_root: Path) -> list[Path]:
    files: list[Path] = []
    for filename in KNOWN_SEC_FILES:
        files.extend(sorted(raw_root.rglob(filename)))
    return files


def print_summary(summary: IngestionSummary, elapsed_seconds: float) -> None:
    print("\nLoaded SEC datasets:")
    for table_name in RAW_TABLES:
        stats = summary.table_stats.get(table_name)
        inserted = stats.inserted if stats else 0
        skipped = stats.skipped if stats else 0
        print(f"- {table_name}: {inserted:,} rows inserted, {skipped:,} skipped")

    years = sorted(set(summary.years_detected))
    year_range = f"{years[0]}–{years[-1]}" if years else "n/a"

    print(f"\nSource folders detected: {len(summary.source_folders):,}")
    print(f"Year range detected: {year_range}")

    if summary.missing_files_by_folder:
        print("\nMissing expected files per folder:")
        for folder_name, files in sorted(summary.missing_files_by_folder.items()):
            print(f"  {folder_name}: {', '.join(files)}")
    else:
        print("\nMissing expected files per folder: none")

    print(f"\nTotal runtime: {elapsed_seconds:.1f}s")


def run_ingestion(
    raw_dir: Path,
    db_path: Path,
    reset: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> IngestionSummary:
    raw_root = raw_dir.resolve()
    if not raw_root.exists():
        raise FileNotFoundError(f"Raw SEC directory not found: {raw_root}")

    tsv_files = collect_tsv_files(raw_root)
    if not tsv_files:
        raise FileNotFoundError(f"No SEC TSV files found under {raw_root}")

    unknown_files = sorted(
        {
            path.name
            for path in raw_root.rglob("*.tsv")
            if not is_known_sec_file(path.name)
        }
    )
    if unknown_files:
        logger.info("ignoring unknown TSV files: %s", ", ".join(unknown_files))

    summary = IngestionSummary()
    source_folders = discover_source_folders(raw_root)
    summary.source_folders = [folder.name for folder in source_folders]
    summary.missing_files_by_folder = audit_missing_files(source_folders)

    conn = get_connection(db_path)
    try:
        ensure_raw_schema(conn, reset=reset)
        for tsv_path in tsv_files:
            table_name = FILE_TO_TABLE[tsv_path.name]
            load_tsv_file(
                tsv_path,
                table_name,
                raw_root,
                conn,
                summary,
                chunk_size=chunk_size,
            )
    finally:
        conn.close()

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest all SEC Form 3/4/5 quarterly TSV datasets into SQLite."
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Root directory containing SEC quarterly folders (default: data/raw/sec).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite database path (default: data/processed/insider_research.db).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop raw tables, recreate schema, and reload all data.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="Pandas read_csv chunksize (default: 50000).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args()
    configure_logging(args.verbose)

    started = time.perf_counter()
    try:
        summary = run_ingestion(
            raw_dir=args.raw_dir,
            db_path=args.db_path,
            reset=args.reset,
            chunk_size=args.chunk_size,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.exception("ingestion failed: %s", exc)
        sys.exit(1)

    print_summary(summary, time.perf_counter() - started)


if __name__ == "__main__":
    main()
