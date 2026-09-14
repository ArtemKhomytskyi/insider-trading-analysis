"""Tests for Phase 4 entity normalization."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.entities.build_entity_dimensions import build_entity_dimensions


CLEAN_DDL = """
CREATE TABLE clean_insider_transactions (
    transaction_id TEXT PRIMARY KEY,
    accession_number TEXT,
    issuer_cik TEXT,
    issuer_name TEXT,
    ticker_clean TEXT,
    is_valid_ticker INTEGER,
    is_invalid_ticker_placeholder INTEGER,
    is_multisymbol_ticker INTEGER,
    insider_cik TEXT,
    insider_name TEXT,
    insider_name_clean TEXT,
    raw_relationship TEXT,
    raw_role_title TEXT,
    normalized_role TEXT,
    role_group TEXT,
    role_weight REAL,
    is_executive INTEGER,
    is_board_member INTEGER,
    is_large_owner INTEGER,
    role_requires_review INTEGER,
    role_confidence TEXT,
    transaction_date TEXT,
    filing_date TEXT,
    transaction_code TEXT,
    is_purchase INTEGER,
    is_sale INTEGER,
    include_mvp_alpha INTEGER,
    source_year INTEGER
);
"""


def row(transaction_id: str, **overrides) -> dict:
    base = {
        "transaction_id": transaction_id,
        "accession_number": transaction_id.split("-")[0],
        "issuer_cik": "0001",
        "issuer_name": "Acme Inc.",
        "ticker_clean": "ACME",
        "is_valid_ticker": 1,
        "is_invalid_ticker_placeholder": 0,
        "is_multisymbol_ticker": 0,
        "insider_cik": "100",
        "insider_name": "Jane Q. Public",
        "insider_name_clean": "JANE Q PUBLIC",
        "raw_relationship": "Officer",
        "raw_role_title": "Chief Financial Officer",
        "normalized_role": "CFO",
        "role_group": "EXECUTIVE",
        "role_weight": 0.95,
        "is_executive": 1,
        "is_board_member": 0,
        "is_large_owner": 0,
        "role_requires_review": 0,
        "role_confidence": "HIGH",
        "transaction_date": "2020-01-15",
        "filing_date": "2020-01-17",
        "transaction_code": "P",
        "is_purchase": 1,
        "is_sale": 0,
        "include_mvp_alpha": 1,
        "source_year": 2020,
    }
    base.update(overrides)
    return base


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / "entity.db")
    connection.row_factory = sqlite3.Row
    connection.executescript(CLEAN_DDL)
    return connection


def insert_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    for item in rows:
        cols = list(item)
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO clean_insider_transactions ({', '.join(cols)}) VALUES ({placeholders})",
            [item[c] for c in cols],
        )
    conn.commit()


def build(conn: sqlite3.Connection, tmp_path: Path, *, limit: int | None = None) -> None:
    build_entity_dimensions(conn, reset=True, export_dir=tmp_path / "exports", limit=limit)


def test_same_insider_cik_multiple_names_one_clean_insider(conn: sqlite3.Connection, tmp_path: Path) -> None:
    insert_rows(
        conn,
        [
            row("a-1", insider_name="Jane Q. Public", insider_name_clean="JANE Q PUBLIC"),
            row("a-2", insider_name="Public, Jane Q.", insider_name_clean="PUBLIC JANE Q", transaction_date="2021-01-15"),
        ],
    )
    build(conn, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM clean_insiders").fetchone()[0] == 1
    insider = conn.execute("SELECT * FROM clean_insiders WHERE insider_cik = '100'").fetchone()
    assert insider["distinct_raw_name_count"] == 2
    assert insider["canonical_name"] == "Public, Jane Q."


def test_same_normalized_name_different_ciks_remain_separate(conn: sqlite3.Connection, tmp_path: Path) -> None:
    insert_rows(
        conn,
        [
            row("a-1", insider_cik="100", insider_name="Jane Public", insider_name_clean="JANE PUBLIC"),
            row("b-1", insider_cik="200", insider_name="Jane Public", insider_name_clean="JANE PUBLIC"),
        ],
    )
    build(conn, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM clean_insiders").fetchone()[0] == 2
    qa = pd.read_csv(tmp_path / "exports" / "insider_same_name_different_cik.csv")
    assert qa.loc[0, "normalized_name"] == "JANE PUBLIC"


def test_company_cik_multiple_names_and_tickers_one_company_with_history(conn: sqlite3.Connection, tmp_path: Path) -> None:
    insert_rows(
        conn,
        [
            row("a-1", issuer_cik="0001", issuer_name="Old Acme Inc.", ticker_clean="OLD", transaction_date="2020-01-15"),
            row("a-2", issuer_cik="0001", issuer_name="Acme Inc.", ticker_clean="ACME", transaction_date="2021-01-15", source_year=2021),
        ],
    )
    build(conn, tmp_path)
    company = conn.execute("SELECT * FROM company_dimension WHERE issuer_cik = '0001'").fetchone()
    assert company["canonical_company_name"] == "Acme Inc."
    assert company["current_or_latest_ticker"] == "ACME"
    assert company["distinct_name_count"] == 2
    assert company["distinct_ticker_count"] == 2
    assert conn.execute("SELECT COUNT(*) FROM company_identifier_history WHERE issuer_cik = '0001'").fetchone()[0] == 2


def test_same_ticker_different_ciks_do_not_merge_companies(conn: sqlite3.Connection, tmp_path: Path) -> None:
    insert_rows(
        conn,
        [
            row("a-1", issuer_cik="0001", issuer_name="Acme Inc.", ticker_clean="ABC"),
            row("b-1", issuer_cik="0002", issuer_name="Beta Inc.", ticker_clean="ABC", insider_cik="200"),
        ],
    )
    build(conn, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM company_dimension").fetchone()[0] == 2
    qa = pd.read_csv(tmp_path / "exports" / "tickers_multiple_ciks.csv")
    assert qa.loc[0, "ticker"] == "ABC"


def test_invalid_ticker_cannot_become_canonical(conn: sqlite3.Connection, tmp_path: Path) -> None:
    insert_rows(
        conn,
        [
            row("a-1", ticker_clean="NONE", is_valid_ticker=0, is_invalid_ticker_placeholder=1),
        ],
    )
    build(conn, tmp_path)
    ticker = conn.execute("SELECT current_or_latest_ticker FROM company_dimension").fetchone()[0]
    assert ticker is None


def test_one_insider_multiple_roles_and_companies(conn: sqlite3.Connection, tmp_path: Path) -> None:
    insert_rows(
        conn,
        [
            row("a-1", issuer_cik="0001", normalized_role="CFO", raw_role_title="CFO"),
            row("a-2", issuer_cik="0001", normalized_role="CEO", raw_role_title="CEO", transaction_date="2021-01-15"),
            row("b-1", issuer_cik="0002", normalized_role="DIRECTOR", role_group="BOARD", raw_role_title="Director", transaction_date="2022-01-15"),
        ],
    )
    build(conn, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM clean_insiders").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM company_dimension").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM insider_company_roles").fetchone()[0] == 3


def test_entity_builder_rerun_produces_same_counts(conn: sqlite3.Connection, tmp_path: Path) -> None:
    insert_rows(conn, [row("a-1"), row("a-2", transaction_date="2021-01-15")])
    build(conn, tmp_path)
    counts1 = [
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("clean_insiders", "company_dimension", "company_identifier_history", "insider_company_roles")
    ]
    build(conn, tmp_path)
    counts2 = [
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("clean_insiders", "company_dimension", "company_identifier_history", "insider_company_roles")
    ]
    assert counts1 == counts2


def test_complete_build_metadata(conn: sqlite3.Connection, tmp_path: Path) -> None:
    insert_rows(conn, [row("a-1")])
    build(conn, tmp_path)
    meta = conn.execute(
        "SELECT status, is_sample, source_clean_row_count FROM entity_build_metadata ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert meta["status"] == "COMPLETE"
    assert meta["is_sample"] == 0
    assert meta["source_clean_row_count"] == 1


def test_sample_build_marked_non_production(conn: sqlite3.Connection, tmp_path: Path) -> None:
    insert_rows(conn, [row("a-1"), row("a-2", insider_cik="200")])
    build(conn, tmp_path, limit=1)
    meta = conn.execute(
        "SELECT status, is_sample, sample_limit FROM entity_build_metadata ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert meta["status"] == "SAMPLE"
    assert meta["is_sample"] == 1
    assert meta["sample_limit"] == 1


def test_deterministic_output_after_rerun(conn: sqlite3.Connection, tmp_path: Path) -> None:
    insert_rows(
        conn,
        [
            row("a-1", insider_name="Jane Public", insider_name_clean="JANE PUBLIC"),
            row("a-2", insider_name="Public, Jane", insider_name_clean="PUBLIC JANE", transaction_date="2021-01-15"),
        ],
    )
    build(conn, tmp_path)
    first = [dict(r) for r in conn.execute("SELECT * FROM clean_insiders ORDER BY insider_cik").fetchall()]
    build(conn, tmp_path)
    second = [dict(r) for r in conn.execute("SELECT * FROM clean_insiders ORDER BY insider_cik").fetchall()]
    for rows in (first, second):
        for item in rows:
            item.pop("created_at", None)
            item.pop("updated_at", None)
    assert first == second
