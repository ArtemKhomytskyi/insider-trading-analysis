"""Tests for SEC ingestion utilities."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.ingestion.sec_ingestion import (
    FILE_TO_TABLE,
    add_row_hashes,
    audit_missing_files,
    compute_row_hash,
    is_known_sec_file,
    map_file_to_table,
    parse_year_quarter,
)


@pytest.mark.parametrize(
    ("folder_name", "expected"),
    [
        ("2006_Q1", (2006, 1)),
        ("2006q1", (2006, 1)),
        ("2006-Q1", (2006, 1)),
        ("2006 Q1", (2006, 1)),
        ("2006_1", (2006, 1)),
        ("2025q1_form345", (2025, 1)),
        ("not_a_period", (None, None)),
    ],
)
def test_parse_year_quarter(folder_name: str, expected: tuple[int | None, int | None]) -> None:
    assert parse_year_quarter(folder_name) == expected


@pytest.mark.parametrize(
    ("filename", "table_name"),
    [
        ("SUBMISSION.tsv", "raw_submission"),
        ("REPORTINGOWNER.tsv", "raw_reporting_owner"),
        ("NONDERIV_TRANS.tsv", "raw_nonderiv_trans"),
        ("DERIV_HOLDING.tsv", "raw_deriv_holding"),
        ("OWNER_SIGNATURE.tsv", "raw_owner_signature"),
    ],
)
def test_file_to_table_mapping(filename: str, table_name: str) -> None:
    assert map_file_to_table(filename) == table_name
    assert FILE_TO_TABLE[filename] == table_name


def test_unknown_files_are_ignored() -> None:
    assert map_file_to_table("README.txt") is None
    assert not is_known_sec_file("CUSTOM.tsv")
    assert is_known_sec_file("FOOTNOTES.tsv")


def test_row_hash_is_stable() -> None:
    values = ["0001234567-24-000001", "4", "AAPL"]
    first = compute_row_hash(values, "2006_Q1/SUBMISSION.tsv", "2006_Q1")
    second = compute_row_hash(values, "2006_Q1/SUBMISSION.tsv", "2006_Q1")
    different = compute_row_hash(values, "2006_Q2/SUBMISSION.tsv", "2006_Q2")

    assert first == second
    assert first != different


def test_row_hash_changes_when_row_values_change() -> None:
    df = pd.DataFrame(
        {"ACCESSION_NUMBER": ["acc-1"], "TRANS_CODE": ["P"]},
    )
    hashes_a = add_row_hashes(
        df,
        ["ACCESSION_NUMBER", "TRANS_CODE"],
        "2006_Q1/NONDERIV_TRANS.tsv",
        "2006_Q1",
    )
    df_changed = df.copy()
    df_changed.loc[0, "TRANS_CODE"] = "S"
    hashes_b = add_row_hashes(
        df_changed,
        ["ACCESSION_NUMBER", "TRANS_CODE"],
        "2006_Q1/NONDERIV_TRANS.tsv",
        "2006_Q1",
    )
    assert hashes_a.iloc[0] != hashes_b.iloc[0]


def test_audit_missing_files(tmp_path: Path) -> None:
    folder = tmp_path / "2006_Q1"
    folder.mkdir()
    (folder / "SUBMISSION.tsv").write_text("ACCESSION_NUMBER\n", encoding="utf-8")

    missing = audit_missing_files([folder])
    assert "2006_Q1" in missing
    assert "REPORTINGOWNER.tsv" in missing["2006_Q1"]
