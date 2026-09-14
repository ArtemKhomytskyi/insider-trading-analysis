"""Basic database utility tests."""

from pathlib import Path

from src.utils.db import PROJECT_ROOT, get_database_path, load_database_config


def test_database_config_path_is_relative_to_project():
    config = load_database_config()
    assert config["database"]["engine"] == "sqlite"
    db_path = get_database_path()
    assert db_path == PROJECT_ROOT / "data" / "processed" / "insider_research.db"


def test_schema_file_exists():
    schema = PROJECT_ROOT / "sql" / "001_create_raw_tables.sql"
    assert schema.exists()
