"""SQLite database utilities for insider-research."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import yaml

from src.ingestion.sec_ingestion import METADATA_COLUMNS, RAW_TABLES

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "database.yaml"
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "sql" / "001_create_raw_tables.sql"


def load_database_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or DEFAULT_CONFIG_PATH
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def get_database_path(config_path: Path | None = None) -> Path:
    config = load_database_config(config_path)
    db_path = Path(config["database"]["path"])
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    return db_path


def get_connection(
    db_path: Path | None = None,
    config_path: Path | None = None,
) -> sqlite3.Connection:
    path = db_path or get_database_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def execute_sql_file(conn: sqlite3.Connection, sql_path: Path) -> None:
    sql_text = sql_path.read_text(encoding="utf-8")
    conn.executescript(sql_text)
    conn.commit()


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [row["name"] for row in rows]


def row_count(conn: sqlite3.Connection, table_name: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_name}").fetchone()
    return int(row["n"])


def drop_raw_tables(conn: sqlite3.Connection) -> None:
    for table_name in [*RAW_TABLES, "ingestion_log"]:
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.commit()


def ensure_raw_schema(
    conn: sqlite3.Connection,
    schema_path: Path | None = None,
    reset: bool = False,
) -> None:
    path = schema_path or DEFAULT_SCHEMA_PATH
    if not path.exists():
        raise FileNotFoundError(f"SQL schema not found: {path}")

    if reset:
        drop_raw_tables(conn)
        execute_sql_file(conn, path)
        return

    if not table_exists(conn, "raw_submission"):
        execute_sql_file(conn, path)
        return

    _migrate_raw_tables(conn)
    _ensure_row_hash_indexes(conn)


def _migrate_raw_tables(conn: sqlite3.Connection) -> None:
    required_metadata = METADATA_COLUMNS
    for table_name in RAW_TABLES:
        if not table_exists(conn, table_name):
            continue
        existing = set(get_table_columns(conn, table_name))
        for column in required_metadata:
            if column not in existing:
                if column in {"source_year", "source_quarter"}:
                    column_type = "INTEGER"
                else:
                    column_type = "TEXT"
                conn.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column} {column_type}"
                )
    conn.commit()


def _ensure_row_hash_indexes(conn: sqlite3.Connection) -> None:
    for table_name in RAW_TABLES:
        index_name = f"idx_{table_name}_row_hash"
        conn.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {index_name}
            ON {table_name} (row_hash)
            """
        )
    conn.commit()


def is_file_ingested(
    conn: sqlite3.Connection,
    source_file: str,
    target_table: str,
) -> bool:
    if not table_exists(conn, "ingestion_log"):
        return False
    row = conn.execute(
        """
        SELECT 1 FROM ingestion_log
        WHERE source_file = ? AND target_table = ?
        """,
        (source_file, target_table),
    ).fetchone()
    return row is not None


def log_ingestion(
    conn: sqlite3.Connection,
    source_file: str,
    target_table: str,
    row_count_value: int,
    source_year: int | None,
    source_quarter: int | None,
    ingested_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO ingestion_log (
            source_file, target_table, row_count,
            source_year, source_quarter, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source_file,
            target_table,
            row_count_value,
            source_year,
            source_quarter,
            ingested_at,
        ),
    )
