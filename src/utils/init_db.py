"""Initialize the SQLite database from SQL schema files."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.utils.db import (
    PROJECT_ROOT,
    ensure_raw_schema,
    get_connection,
    get_database_path,
)


def init_database(sql_file: Path | None = None, reset: bool = False) -> Path:
    sql_path = sql_file or (PROJECT_ROOT / "sql" / "001_create_raw_tables.sql")
    if not sql_path.exists():
        raise FileNotFoundError(f"SQL schema not found: {sql_path}")

    db_path = get_database_path()
    print(f"initializing database: {db_path}")

    conn = get_connection(db_path)
    try:
        ensure_raw_schema(conn, schema_path=sql_path, reset=reset)
    finally:
        conn.close()

    print(f"schema applied: {sql_path.name}")

    conn = get_connection(db_path)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        print("tables:")
        for row in tables:
            print(f"  - {row['name']}")
    finally:
        conn.close()

    return db_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize insider-research SQLite DB.")
    parser.add_argument(
        "--sql",
        type=Path,
        default=None,
        help="Path to SQL schema file (default: sql/001_create_raw_tables.sql).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate raw tables before applying schema.",
    )
    args = parser.parse_args()
    init_database(args.sql, reset=args.reset)


if __name__ == "__main__":
    main()
