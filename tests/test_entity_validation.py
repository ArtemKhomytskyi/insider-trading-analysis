"""Tests for Phase 4 entity validation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.analytics.validate_entity_dimensions import linkage_coverage, run_validation
from src.entities.build_entity_dimensions import build_entity_dimensions
from tests.test_entity_normalization import CLEAN_DDL, insert_rows, row


@pytest.fixture
def built_conn(tmp_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / "validation.db")
    connection.row_factory = sqlite3.Row
    connection.executescript(CLEAN_DDL)
    insert_rows(
        connection,
        [
            row("a-1", issuer_cik="0001", insider_cik="100"),
            row("b-1", issuer_cik="0002", insider_cik="200", ticker_clean="BETA", insider_name="John Smith", insider_name_clean="JOHN SMITH"),
        ],
    )
    build_entity_dimensions(connection, reset=True, export_dir=tmp_path / "exports")
    return connection


def test_relationship_foreign_keys_valid(built_conn: sqlite3.Connection, tmp_path: Path) -> None:
    summary = run_validation(built_conn, tmp_path / "validation")
    assert (tmp_path / "validation" / "entity_validation_summary.csv").exists()
    assert (tmp_path / "validation" / "entity_linkage_coverage.csv").exists()
    fk_checks = summary[summary["check_name"].isin(["role_orphan_insider", "role_orphan_company"])]
    assert set(fk_checks["severity"]) == {"PASS"}


def test_validator_detects_orphan_relationship(built_conn: sqlite3.Connection, tmp_path: Path) -> None:
    built_conn.execute(
        """
        INSERT INTO insider_company_roles (
            insider_cik, issuer_cik, normalized_role, role_group, role_weight,
            is_executive, is_board_member, is_large_owner, raw_role_title,
            raw_relationship, first_seen_date, last_seen_date, filing_count,
            transaction_count, role_requires_review, role_confidence
        ) VALUES ('missing', '0001', 'CEO', 'EXECUTIVE', 1, 1, 0, 0, 'CEO',
                  'Officer', '2020-01-01', '2020-01-01', 1, 1, 0, 'HIGH')
        """
    )
    built_conn.commit()
    summary = run_validation(built_conn, tmp_path / "validation")
    orphan = summary.loc[summary["check_name"] == "role_orphan_insider"].iloc[0]
    assert orphan["severity"] == "FAIL"


def test_linkage_coverage_reports_100_percent_for_mvp(built_conn: sqlite3.Connection) -> None:
    coverage = linkage_coverage(built_conn)
    value = coverage.loc[coverage["metric"] == "mvp_transactions_linked_to_both_pct", "value"].iloc[0]
    assert value == 100.0


def test_validation_rejects_incomplete_build(built_conn: sqlite3.Connection, tmp_path: Path) -> None:
    built_conn.execute(
        """
        INSERT INTO entity_build_metadata (
            build_id, started_at, completed_at, status, source_clean_row_count,
            source_max_rowid, builder_version, is_sample, sample_limit, error_message
        ) VALUES ('in-progress', '2099-01-01T00:00:00+00:00', NULL, 'IN_PROGRESS', 2, 2, 'test', 0, NULL, NULL)
        """
    )
    built_conn.commit()
    summary = run_validation(built_conn, tmp_path / "validation")
    status = summary.loc[summary["check_name"] == "entity_build_status_complete"].iloc[0]
    assert status["severity"] == "FAIL"
    assert status["metric_value"] == "IN_PROGRESS"


def test_validation_rejects_sample_build(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "sample.db")
    connection.row_factory = sqlite3.Row
    connection.executescript(CLEAN_DDL)
    insert_rows(connection, [row("a-1"), row("a-2", insider_cik="200")])
    build_entity_dimensions(connection, reset=True, export_dir=tmp_path / "exports", limit=1)
    summary = run_validation(connection, tmp_path / "validation")
    status = summary.loc[summary["check_name"] == "entity_build_status_complete"].iloc[0]
    assert status["severity"] == "FAIL"
    assert status["metric_value"] == "SAMPLE"
