"""Tests for SEC file structure audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ingestion.audit_sec_file_structure import audit_file_type, determine_status
from src.ingestion.sec_ingestion import (
    compare_columns_to_canonical,
    find_duplicate_columns,
    read_tsv_header,
    select_canonical_columns,
    suggest_renamed_columns,
)
from src.ingestion.sec_ingestion import FileHeaderInfo


def _write_header(path: Path, columns: list[str]) -> None:
    path.write_text("\t".join(columns) + "\n", encoding="utf-8")


def _header(
    folder: str,
    columns: list[str],
    file_type: str = "SUBMISSION.tsv",
) -> FileHeaderInfo:
    return FileHeaderInfo(
        file_type=file_type,
        source_folder=folder,
        source_year=2006,
        source_quarter=1,
        file_path=f"{folder}/{file_type}",
        columns=columns,
        column_count=len(columns),
        schema_hash="",
        read_status="ok",
    )


def test_identical_schemas_return_ok(tmp_path: Path) -> None:
    cols = ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE"]
    for name in ("2006_Q1", "2006_Q2"):
        folder = tmp_path / name
        folder.mkdir()
        _write_header(folder / "SUBMISSION.tsv", cols)

    result = audit_file_type(
        "SUBMISSION.tsv",
        sorted((tmp_path / "2006_Q1" / "SUBMISSION.tsv", tmp_path / "2006_Q2" / "SUBMISSION.tsv")),
        tmp_path,
    )
    assert result.status == "OK"
    assert result.unique_schema_versions == 1
    assert result.folders_with_missing == 0
    assert result.folders_with_extra == 0


def test_missing_column_detected(tmp_path: Path) -> None:
    base = ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE"]
    paths = []
    for name in ("2006_Q1", "2006_Q2", "2006_Q3"):
        folder = tmp_path / name
        folder.mkdir()
        cols = base if name != "2006_Q3" else ["ACCESSION_NUMBER", "FILING_DATE"]
        path = folder / "SUBMISSION.tsv"
        _write_header(path, cols)
        paths.append(path)

    result = audit_file_type("SUBMISSION.tsv", paths, tmp_path)
    assert result.status == "REVIEW"
    assert result.folders_with_missing >= 1

    missing, extra, _ = compare_columns_to_canonical(
        ["ACCESSION_NUMBER", "FILING_DATE"],
        result.canonical_columns,
    )
    assert "DOCUMENT_TYPE" in missing
    assert not extra


def test_extra_column_detected(tmp_path: Path) -> None:
    base = ["ACCESSION_NUMBER", "FILING_DATE"]
    paths = []
    for name in ("2006_Q1", "2006_Q2", "2006_Q3"):
        folder = tmp_path / name
        folder.mkdir()
        cols = base if name != "2006_Q3" else base + ["EXTRA_COL"]
        path = folder / "SUBMISSION.tsv"
        _write_header(path, cols)
        paths.append(path)

    result = audit_file_type("SUBMISSION.tsv", paths, tmp_path)
    assert result.status == "REVIEW"
    assert result.folders_with_extra >= 1


def test_column_order_difference_detected() -> None:
    canonical = ["A", "B", "C"]
    reordered = ["B", "A", "C"]
    missing, extra, order_differs = compare_columns_to_canonical(reordered, canonical)
    assert not missing
    assert not extra
    assert order_differs


def test_duplicate_columns_detected() -> None:
    columns = ["A", "B", "A"]
    assert find_duplicate_columns(columns) == ["A"]


def test_empty_file_handled(tmp_path: Path) -> None:
    folder = tmp_path / "2006_Q1"
    folder.mkdir()
    (folder / "SUBMISSION.tsv").write_text("", encoding="utf-8")

    columns, status, error = read_tsv_header(folder / "SUBMISSION.tsv")
    assert columns == []
    assert status == "empty"
    assert error is None

    result = audit_file_type(
        "SUBMISSION.tsv",
        [folder / "SUBMISSION.tsv"],
        tmp_path,
    )
    assert result.empty_files == 1
    assert result.status == "REVIEW"


def test_unreadable_file_handled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "2006_Q1"
    folder.mkdir()
    tsv_path = folder / "SUBMISSION.tsv"
    tsv_path.write_text("ACCESSION_NUMBER\n", encoding="utf-8")

    def fail_open(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("src.ingestion.sec_ingestion.Path.open", fail_open)

    columns, status, error = read_tsv_header(tsv_path)
    assert status == "unreadable"
    assert error is not None


def test_renamed_column_candidate() -> None:
    suggestions = suggest_renamed_columns(
        ["AFF10B5ONE"],
        ["AFF_10B5ONE"],
    )
    assert suggestions == {"AFF10B5ONE": "AFF_10B5ONE"}


def test_select_canonical_uses_majority() -> None:
    schema_a = ["A", "B", "C"]
    schema_b = ["A", "B"]
    headers = [
        _header("f1", schema_a),
        _header("f2", schema_a),
        _header("f3", schema_b),
    ]
    for header in headers:
        from src.ingestion.sec_ingestion import compute_schema_hash

        header.schema_hash = compute_schema_hash(header.columns)

    canonical = select_canonical_columns(headers)
    assert canonical == schema_a


def test_determine_status_ok_for_clean_headers() -> None:
    from src.ingestion.audit_sec_file_structure import FileTypeAuditResult

    cols = ["A", "B"]
    headers = [_header("f1", cols), _header("f2", cols)]
    for header in headers:
        from src.ingestion.sec_ingestion import compute_schema_hash

        header.schema_hash = compute_schema_hash(header.columns)

    result = FileTypeAuditResult(
        file_type="SUBMISSION.tsv",
        headers=headers,
        canonical_columns=cols,
        schema_versions={"x": {"columns": cols, "folders": ["f1", "f2"], "count": 2}},
    )
    assert determine_status(result) == "OK"
