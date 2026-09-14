"""Tests for clean layer validation (Phase 3.5)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.analytics.validate_clean_layer import (
    VALIDATION_SUMMARY_COLUMNS,
    ValidationCheck,
    checks_to_dataframe,
    compute_mvp_integrity_violations,
    compute_survival_funnel,
    load_validation_config,
    run_validation,
    severity_from_count,
    severity_from_pct,
    table_exists,
)


CLEAN_DDL = """
CREATE TABLE clean_insider_transactions (
    transaction_id TEXT PRIMARY KEY,
    accession_number TEXT,
    nonderiv_trans_sk TEXT,
    document_type TEXT,
    is_form_3 INTEGER,
    is_form_4 INTEGER,
    is_form_5 INTEGER,
    is_amendment INTEGER,
    issuer_cik TEXT,
    issuer_name TEXT,
    ticker TEXT,
    ticker_raw TEXT,
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
    period_of_report TEXT,
    filing_lag_days INTEGER,
    is_stale_filing INTEGER,
    filing_lag_bucket TEXT,
    transaction_year INTEGER,
    filing_year INTEGER,
    is_pre_source_transaction_date INTEGER,
    transaction_year_source_gap INTEGER,
    transaction_code TEXT,
    transaction_meaning TEXT,
    transaction_category TEXT,
    signal_direction TEXT,
    signal_quality TEXT,
    transaction_include_clean_mvp INTEGER,
    security_title_raw TEXT,
    security_title_normalized TEXT,
    normalized_security_type TEXT,
    security_include_clean_mvp INTEGER,
    security_requires_review INTEGER,
    security_confidence TEXT,
    acquired_disposed_code TEXT,
    direct_indirect_ownership TEXT,
    nature_of_ownership TEXT,
    shares REAL,
    price_per_share REAL,
    shares_owned_following REAL,
    transaction_value REAL,
    is_purchase INTEGER,
    is_sale INTEGER,
    is_open_market INTEGER,
    is_missing_price INTEGER,
    is_zero_price INTEGER,
    is_negative_or_zero_shares INTEGER,
    is_missing_transaction_date INTEGER,
    is_missing_filing_date INTEGER,
    is_filing_before_transaction INTEGER,
    is_extreme_transaction_value INTEGER,
    is_tiny_transaction INTEGER,
    is_valid_clean_event INTEGER,
    include_mvp_alpha INTEGER,
    source_year INTEGER,
    source_quarter INTEGER,
    source_file TEXT,
    source_folder TEXT,
    created_at TEXT
);
CREATE TABLE raw_nonderiv_trans (ACCESSION_NUMBER TEXT);
CREATE TABLE raw_submission (ACCESSION_NUMBER TEXT);
CREATE TABLE raw_reporting_owner (ACCESSION_NUMBER TEXT);
"""


def _mvp_row(
    transaction_id: str,
    *,
    trans_code: str = "P",
    security_type: str = "COMMON_EQUITY",
    include_mvp: int = 1,
    value: float = 50_000.0,
    **kwargs,
) -> dict:
    base = {
        "transaction_id": transaction_id,
        "accession_number": "acc-1",
        "nonderiv_trans_sk": "1",
        "document_type": "4",
        "is_form_3": 0,
        "is_form_4": 1,
        "is_form_5": 0,
        "is_amendment": 0,
        "ticker": "AAPL",
        "ticker_raw": "AAPL",
        "ticker_clean": "AAPL",
        "is_valid_ticker": 1,
        "is_invalid_ticker_placeholder": 0,
        "is_multisymbol_ticker": 0,
        "insider_cik": "123",
        "filing_lag_bucket": "THREE_TO_SEVEN_DAYS",
        "is_stale_filing": 0,
        "transaction_year": 2020,
        "filing_year": 2020,
        "is_pre_source_transaction_date": 0,
        "transaction_year_source_gap": 0,
        "transaction_date": "2020-01-15",
        "filing_date": "2020-01-18",
        "filing_lag_days": 3,
        "transaction_code": trans_code,
        "transaction_include_clean_mvp": 1,
        "normalized_security_type": security_type,
        "security_include_clean_mvp": 1 if security_type == "COMMON_EQUITY" else 0,
        "normalized_role": "CEO",
        "role_group": "EXECUTIVE",
        "role_weight": 1.0,
        "is_executive": 1,
        "is_board_member": 0,
        "shares": 1000.0,
        "price_per_share": 50.0,
        "transaction_value": value,
        "is_valid_clean_event": 1,
        "include_mvp_alpha": include_mvp,
    }
    base.update(kwargs)
    return base


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(CLEAN_DDL)
    connection.executemany(
        "INSERT INTO raw_nonderiv_trans VALUES (?)",
        [("acc-1",), ("acc-2",)],
    )
    connection.executemany(
        "INSERT INTO raw_submission VALUES (?)",
        [("acc-1",)],
    )
    connection.executemany(
        "INSERT INTO raw_reporting_owner VALUES (?)",
        [("acc-1",)],
    )
    return connection


def _insert_row(conn: sqlite3.Connection, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO clean_insider_transactions ({', '.join(cols)}) VALUES ({placeholders})"
    conn.execute(sql, [row[c] for c in cols])
    conn.commit()


def test_validation_summary_schema() -> None:
    check = ValidationCheck(
        "T01",
        "test_check",
        "PASS",
        "0",
        "0",
        "desc",
        "action",
    )
    df = checks_to_dataframe([check])
    assert list(df.columns) == list(VALIDATION_SUMMARY_COLUMNS)


def test_severity_logic() -> None:
    assert severity_from_count(0) == "PASS"
    assert severity_from_count(1, fail_if_positive=True) == "FAIL"
    assert severity_from_count(1, fail_if_positive=False, warn_if_positive=True) == "WARN"
    assert severity_from_pct(10, warn_threshold=5) == "WARN"
    assert severity_from_pct(3, warn_threshold=5) == "PASS"


def test_mvp_integrity_catches_violations(conn: sqlite3.Connection) -> None:
    _insert_row(conn, _mvp_row("good-1"))
    _insert_row(
        conn,
        _mvp_row(
            "bad-form3",
            include_mvp=1,
            is_form_4=0,
            is_form_3=1,
            document_type="3",
        ),
    )
    _insert_row(
        conn,
        _mvp_row(
            "bad-code",
            trans_code="A",
            transaction_include_clean_mvp=0,
        ),
    )
    _insert_row(
        conn,
        _mvp_row(
            "bad-security",
            security_type="STOCK_COMPENSATION",
            security_include_clean_mvp=0,
        ),
    )
    config = load_validation_config(
        Path(__file__).resolve().parents[1] / "configs" / "cleaning.yaml"
    )
    violations = compute_mvp_integrity_violations(conn, config)
    assert violations.loc[violations["violation_type"] == "not_form_4", "violation_count"].iloc[0] >= 1
    assert (
        violations.loc[
            violations["violation_type"] == "transaction_code_not_ps", "violation_count"
        ].iloc[0]
        >= 1
    )
    assert (
        violations.loc[
            violations["violation_type"] == "security_not_common_equity", "violation_count"
        ].iloc[0]
        >= 1
    )


def test_survival_funnel_ordered_stages(conn: sqlite3.Connection) -> None:
    _insert_row(conn, _mvp_row("f1", include_mvp=1))
    _insert_row(
        conn,
        _mvp_row(
            "f2",
            include_mvp=0,
            is_valid_clean_event=0,
            trans_code="A",
            transaction_include_clean_mvp=0,
            value=100.0,
        ),
    )
    config = load_validation_config(
        Path(__file__).resolve().parents[1] / "configs" / "cleaning.yaml"
    )
    funnel = compute_survival_funnel(conn, config)
    assert list(funnel["stage_id"]) == [
        "1_total_clean_rows",
        "2_form_4_rows",
        "3_non_amendment_form_4",
        "4_ps_transaction_codes",
        "5_common_equity",
        "6_valid_clean_event",
        "7_value_above_minimum",
        "8_include_mvp_alpha",
    ]
    counts = funnel["row_count"].tolist()
    assert counts[0] >= counts[-1]
    assert counts[-1] == 1


def test_duplicate_transaction_id_detection(conn: sqlite3.Connection) -> None:
    _insert_row(conn, _mvp_row("dup-1"))
    dup_count = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT transaction_id FROM clean_insider_transactions
            GROUP BY transaction_id HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    assert dup_count == 0
    assert severity_from_count(1, fail_if_positive=True) == "FAIL"


def test_run_validation_exports(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _noop_chart(*_args, **_kwargs) -> Path:
        return tmp_path / "noop.png"

    monkeypatch.setattr(
        "src.analytics.validate_clean_layer.save_chart",
        _noop_chart,
    )
    _insert_row(conn, _mvp_row("run-1"))
    out_dir = tmp_path / "validation"
    config = load_validation_config(
        Path(__file__).resolve().parents[1] / "configs" / "cleaning.yaml"
    )
    summary = run_validation(conn, config, out_dir)
    assert table_exists(conn, "clean_insider_transactions")
    assert (out_dir / "validation_summary.csv").exists()
    assert (out_dir / "row_count_summary.csv").exists()
    assert (out_dir / "filter_survival_funnel.csv").exists()
    assert "severity" in summary.columns
    assert set(summary["severity"]).issubset({"PASS", "WARN", "FAIL"})
