"""Tests for Phase 5.1 historical security mapping."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.mapping.build_security_mapping import build_security_mapping
from src.analytics.validate_security_mapping import run_validation


DDL = """
CREATE TABLE company_dimension (
    issuer_cik TEXT PRIMARY KEY,
    canonical_company_name TEXT,
    current_or_latest_ticker TEXT,
    first_seen_date TEXT,
    last_seen_date TEXT,
    first_filing_date TEXT,
    last_filing_date TEXT,
    transaction_count INTEGER,
    mvp_transaction_count INTEGER,
    unique_insider_count INTEGER,
    distinct_name_count INTEGER,
    distinct_ticker_count INTEGER,
    sector TEXT,
    industry TEXT,
    exchange TEXT,
    market_cap REAL,
    country TEXT,
    currency TEXT,
    metadata_source TEXT,
    metadata_updated_at TEXT,
    company_requires_review INTEGER,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE company_identifier_history (
    issuer_cik TEXT,
    ticker TEXT,
    company_name TEXT,
    first_seen_date TEXT,
    last_seen_date TEXT,
    first_source_year INTEGER,
    last_source_year INTEGER,
    occurrence_count INTEGER,
    is_latest_ticker INTEGER,
    is_latest_name INTEGER,
    ticker_is_valid INTEGER,
    ticker_is_multisymbol INTEGER
);
CREATE TABLE clean_insider_transactions (
    transaction_id TEXT PRIMARY KEY,
    issuer_cik TEXT,
    transaction_date TEXT,
    filing_date TEXT,
    ticker_clean TEXT,
    security_title_raw TEXT,
    include_mvp_alpha INTEGER,
    transaction_code TEXT,
    transaction_value REAL,
    insider_cik TEXT
);
"""


def add_company(conn: sqlite3.Connection, cik: str, ticker: str = "AAA") -> None:
    conn.execute(
        """
        INSERT INTO company_dimension (
            issuer_cik, canonical_company_name, current_or_latest_ticker,
            transaction_count, mvp_transaction_count, unique_insider_count,
            distinct_name_count, distinct_ticker_count, company_requires_review
        ) VALUES (?, ?, ?, 1, 1, 1, 1, 1, 0)
        """,
        (cik, f"Company {cik}", ticker),
    )


def add_history(
    conn: sqlite3.Connection,
    cik: str,
    ticker: str,
    start: str,
    end: str,
    *,
    latest: int = 1,
    valid: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO company_identifier_history VALUES (?, ?, ?, ?, ?, 2020, 2021, 1, ?, 1, ?, 0)
        """,
        (cik, ticker, f"Company {cik}", start, end, latest, valid),
    )


def add_tx(
    conn: sqlite3.Connection,
    tid: str,
    cik: str,
    date: str,
    ticker: str,
    *,
    title: str = "Common Stock",
    mvp: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO clean_insider_transactions VALUES (?, ?, ?, ?, ?, ?, ?, 'P', 10000, 'owner')",
        (tid, cik, date, date, ticker, title, mvp),
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / "mapping.db")
    connection.row_factory = sqlite3.Row
    connection.executescript(DDL)
    return connection


def test_one_cik_one_ticker_maps_transaction(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_company(conn, "1", "AAA")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-12-31")
    add_tx(conn, "t1", "1", "2020-06-01", "AAA")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    mapped = conn.execute("SELECT * FROM transaction_market_mapping WHERE transaction_id='t1'").fetchone()
    assert mapped["is_mapped"] == 1
    assert mapped["market_data_symbol"] == "AAA"


def test_ticker_change_is_point_in_time(conn: sqlite3.Connection) -> None:
    add_company(conn, "1", "NEW")
    add_history(conn, "1", "OLD", "2020-01-01", "2020-12-31", latest=0)
    add_history(conn, "1", "NEW", "2021-01-01", "2021-12-31", latest=1)
    add_tx(conn, "before", "1", "2020-06-01", "OLD")
    add_tx(conn, "after", "1", "2021-06-01", "NEW")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    before = conn.execute("SELECT market_data_symbol FROM transaction_market_mapping WHERE transaction_id='before'").fetchone()[0]
    after = conn.execute("SELECT market_data_symbol FROM transaction_market_mapping WHERE transaction_id='after'").fetchone()[0]
    assert before == "OLD"
    assert after == "NEW"


def test_same_ticker_multiple_ciks_not_merged(conn: sqlite3.Connection) -> None:
    add_company(conn, "1", "AAA")
    add_company(conn, "2", "AAA")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-12-31")
    add_history(conn, "2", "AAA", "2021-01-01", "2021-12-31")
    add_tx(conn, "t1", "1", "2020-06-01", "AAA")
    add_tx(conn, "t2", "2", "2021-06-01", "AAA")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    assert conn.execute("SELECT COUNT(DISTINCT issuer_cik) FROM security_identifier_mapping WHERE ticker_clean='AAA'").fetchone()[0] == 2
    assert conn.execute("SELECT SUM(is_mapped) FROM transaction_market_mapping").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM transaction_market_mapping WHERE issuer_cik NOT IN ('1','2')").fetchone()[0] == 0


def test_overlapping_intervals_resolve_on_exact_transaction_ticker(conn: sqlite3.Connection) -> None:
    add_company(conn, "1", "AAA")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-12-31")
    add_history(conn, "1", "AAB", "2020-06-01", "2021-12-31")
    add_tx(conn, "t1", "1", "2020-07-01", "AAA")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    row = conn.execute(
        "SELECT is_mapped, is_ambiguous, market_data_symbol, resolution_method "
        "FROM transaction_market_mapping WHERE transaction_id='t1'"
    ).fetchone()
    assert row["is_mapped"] == 1
    assert row["is_ambiguous"] == 0
    assert row["market_data_symbol"] == "AAA"
    assert row["resolution_method"] == "CIK_DATE_TICKER_EXACT"


def test_overlapping_intervals_without_exact_ticker_remain_ambiguous(conn: sqlite3.Connection) -> None:
    add_company(conn, "1", "AAA")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-12-31")
    add_history(conn, "1", "AAB", "2020-06-01", "2021-12-31")
    add_tx(conn, "t1", "1", "2020-07-01", "AAC")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    row = conn.execute(
        "SELECT is_mapped, is_ambiguous, ambiguity_reason "
        "FROM transaction_market_mapping WHERE transaction_id='t1'"
    ).fetchone()
    assert row["is_mapped"] == 0
    assert row["is_ambiguous"] == 1
    assert row["ambiguity_reason"] == "NO_EXACT_TICKER_MATCH"


def test_exact_transaction_ticker_resolves_multiple_tickers_same_date(conn: sqlite3.Connection) -> None:
    add_company(conn, "1", "AAB")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-12-31", latest=0)
    add_history(conn, "1", "AAB", "2020-01-01", "2020-12-31", latest=1)
    add_tx(conn, "t1", "1", "2020-06-01", "AAA")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    row = conn.execute("SELECT is_mapped, market_data_symbol, resolution_method FROM transaction_market_mapping WHERE transaction_id='t1'").fetchone()
    assert row["is_mapped"] == 1
    assert row["market_data_symbol"] == "AAA"
    assert row["resolution_method"] == "CIK_DATE_TICKER_EXACT"


def test_invalid_ticker_not_accepted(conn: sqlite3.Connection) -> None:
    add_company(conn, "1", None)
    add_history(conn, "1", "NONE", "2020-01-01", "2020-12-31", valid=0)
    add_tx(conn, "t1", "1", "2020-06-01", "NONE")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    assert conn.execute("SELECT market_data_symbol FROM security_identifier_mapping").fetchone()[0] is None
    assert conn.execute("SELECT is_mapped FROM transaction_market_mapping").fetchone()[0] == 0


def test_share_class_ambiguity_flagged(conn: sqlite3.Connection) -> None:
    add_company(conn, "1", "AAA")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-12-31")
    add_tx(conn, "t1", "1", "2020-06-01", "AAA", title="Class A Common Stock")
    add_tx(conn, "t2", "1", "2020-06-02", "AAA", title="Class B Common Stock")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    assert conn.execute("SELECT requires_review FROM security_identifier_mapping").fetchone()[0] == 1


def test_mapping_validation_passes_for_valid_build(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_company(conn, "1", "AAA")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-12-31")
    add_tx(conn, "t1", "1", "2020-06-01", "AAA")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    summary = run_validation(conn, tmp_path)
    assert not (summary["severity"] == "FAIL").any()


def test_duplicate_source_identifier_records_deduplicated(conn: sqlite3.Connection) -> None:
    add_company(conn, "1", "AAA")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-01-01")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-01-01")
    conn.execute(
        "UPDATE company_identifier_history SET company_name = 'Old Name' WHERE rowid = 1"
    )
    conn.execute(
        "UPDATE company_identifier_history SET company_name = 'New Name' WHERE rowid = 2"
    )
    add_tx(conn, "t1", "1", "2020-01-01", "AAA")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    assert conn.execute("SELECT COUNT(*) FROM security_identifier_mapping").fetchone()[0] == 1
    mapping = conn.execute("SELECT requires_review, is_ambiguous FROM security_identifier_mapping").fetchone()
    assert mapping["requires_review"] == 1
    assert mapping["is_ambiguous"] == 0


def test_same_cik_ticker_distinct_intervals_get_distinct_mapping_ids(conn: sqlite3.Connection) -> None:
    add_company(conn, "1", "AAA")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-06-30", latest=0)
    add_history(conn, "1", "AAA", "2020-07-01", "2020-12-31", latest=1)
    add_tx(conn, "t1", "1", "2020-03-01", "AAA")
    add_tx(conn, "t2", "1", "2020-09-01", "AAA")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    ids = [row[0] for row in conn.execute("SELECT mapping_id FROM security_identifier_mapping ORDER BY valid_from")]
    assert len(ids) == 2
    assert len(set(ids)) == 2


def test_mapping_id_deterministic_across_reruns(conn: sqlite3.Connection) -> None:
    add_company(conn, "1", "AAA")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-12-31")
    add_tx(conn, "t1", "1", "2020-06-01", "AAA")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    first = conn.execute("SELECT mapping_id FROM security_identifier_mapping").fetchone()[0]
    build_security_mapping(conn, reset=True, export=False)
    second = conn.execute("SELECT mapping_id FROM security_identifier_mapping").fetchone()[0]
    assert first == second


def test_validation_rejects_failed_build_metadata(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_company(conn, "1", "AAA")
    add_history(conn, "1", "AAA", "2020-01-01", "2020-12-31")
    add_tx(conn, "t1", "1", "2020-06-01", "AAA")
    conn.commit()
    build_security_mapping(conn, reset=True, export=False)
    conn.execute(
        """
        INSERT INTO security_mapping_build_metadata (
            build_id, started_at, completed_at, status, source_identifier_row_count,
            mapping_row_count, transaction_mapping_row_count, builder_version,
            is_sample, sample_limit, error_message
        ) VALUES ('failed', '2099-01-01T00:00:00+00:00', '2099-01-01T00:00:01+00:00',
                  'FAILED', 1, 0, 0, 'test', 0, NULL, 'boom')
        """
    )
    conn.commit()
    summary = run_validation(conn, tmp_path)
    row = summary.loc[summary["check_name"] == "security_mapping_build_complete"].iloc[0]
    assert row["severity"] == "FAIL"
    assert row["metric_value"] == "FAILED"
