"""
Phase 1.2: audit SEC TSV file structure consistency across quarterly folders.

Reads header rows only (no data rows, no database access).
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.ingestion.sec_ingestion import (
    KNOWN_SEC_FILES,
    FileHeaderInfo,
    build_schema_versions,
    compare_columns_to_canonical,
    compute_schema_hash,
    detect_casing_variants,
    discover_sec_tsv_files,
    find_duplicate_columns,
    find_whitespace_columns,
    parse_year_quarter_from_path,
    read_tsv_header,
    relative_to_raw,
    select_canonical_columns,
    suggest_renamed_columns,
    write_json,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw" / "sec"
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "exports" / "schema_audit"


@dataclass
class FileTypeAuditResult:
    file_type: str
    headers: list[FileHeaderInfo] = field(default_factory=list)
    canonical_columns: list[str] = field(default_factory=list)
    all_detected_columns: list[str] = field(default_factory=list)
    schema_versions: dict = field(default_factory=dict)
    casing_variants: dict = field(default_factory=dict)
    renamed_candidates: list[dict] = field(default_factory=list)
    status: str = "OK"

    @property
    def folders_detected(self) -> int:
        return len(self.headers)

    @property
    def unique_schema_versions(self) -> int:
        return len(self.schema_versions)

    @property
    def folders_with_missing(self) -> int:
        return sum(1 for h in self.headers if h.read_status == "ok" and _has_missing(h, self))

    @property
    def folders_with_extra(self) -> int:
        return sum(1 for h in self.headers if h.read_status == "ok" and _has_extra(h, self))

    @property
    def folders_with_order_diff(self) -> int:
        return sum(1 for h in self.headers if h.read_status == "ok" and _has_order_diff(h, self))

    @property
    def empty_files(self) -> int:
        return sum(1 for h in self.headers if h.read_status == "empty")

    @property
    def unreadable_files(self) -> int:
        return sum(1 for h in self.headers if h.read_status == "unreadable")


def _has_missing(header: FileHeaderInfo, result: FileTypeAuditResult) -> bool:
    missing, _, _ = _compare(header, result.canonical_columns)
    return bool(missing)


def _has_extra(header: FileHeaderInfo, result: FileTypeAuditResult) -> bool:
    _, extra, _ = _compare(header, result.canonical_columns)
    return bool(extra)


def _has_order_diff(header: FileHeaderInfo, result: FileTypeAuditResult) -> bool:
    _, _, order_differs = _compare(header, result.canonical_columns)
    return order_differs


def _compare(header: FileHeaderInfo, canonical: list[str]) -> tuple[list[str], list[str], bool]:
    if header.read_status != "ok" or not canonical:
        return [], [], False
    return compare_columns_to_canonical(header.columns, canonical)


def read_header_info(
    tsv_path: Path,
    file_type: str,
    raw_root: Path,
) -> FileHeaderInfo:
    source_folder = tsv_path.parent.name
    source_year, source_quarter = parse_year_quarter_from_path(tsv_path)
    file_path = relative_to_raw(tsv_path, raw_root)

    columns, read_status, error_message = read_tsv_header(tsv_path)
    duplicates = find_duplicate_columns(columns) if read_status == "ok" else []
    whitespace = find_whitespace_columns(columns) if read_status == "ok" else []

    if read_status == "ok" and duplicates:
        read_status = "ok"

    return FileHeaderInfo(
        file_type=file_type,
        source_folder=source_folder,
        source_year=source_year,
        source_quarter=source_quarter,
        file_path=file_path,
        columns=columns,
        column_count=len(columns),
        schema_hash=compute_schema_hash(columns) if read_status == "ok" else "",
        duplicate_columns=duplicates,
        whitespace_columns=whitespace,
        read_status=read_status,
        error_message=error_message,
    )


def audit_file_type(file_type: str, paths: list[Path], raw_root: Path) -> FileTypeAuditResult:
    result = FileTypeAuditResult(file_type=file_type)

    for tsv_path in paths:
        header = read_header_info(tsv_path, file_type, raw_root)
        result.headers.append(header)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "%s | %s | status=%s | cols=%s",
                file_type,
                header.source_folder,
                header.read_status,
                header.column_count,
            )

    ok_headers = [h for h in result.headers if h.read_status == "ok"]
    result.canonical_columns = select_canonical_columns(ok_headers)
    result.schema_versions = build_schema_versions(ok_headers)
    result.casing_variants = detect_casing_variants(ok_headers)

    all_columns: set[str] = set()
    for header in ok_headers:
        all_columns.update(header.columns)
    result.all_detected_columns = sorted(all_columns)

    for header in ok_headers:
        missing, extra, _ = compare_columns_to_canonical(
            header.columns, result.canonical_columns
        )
        renamed = suggest_renamed_columns(missing, extra)
        for expected, candidate in renamed.items():
            result.renamed_candidates.append(
                {
                    "source_folder": header.source_folder,
                    "expected_column": expected,
                    "candidate_column": candidate,
                }
            )

    result.status = determine_status(result)
    return result


def determine_status(result: FileTypeAuditResult) -> str:
    if result.unreadable_files or result.empty_files:
        return "REVIEW"
    if result.unique_schema_versions > 1:
        return "REVIEW"
    if result.folders_with_missing or result.folders_with_extra:
        return "REVIEW"
    if result.folders_with_order_diff:
        return "REVIEW"
    if any(h.duplicate_columns for h in result.headers):
        return "REVIEW"
    if any(h.whitespace_columns for h in result.headers):
        return "REVIEW"
    if result.casing_variants:
        return "REVIEW"
    if result.renamed_candidates:
        return "REVIEW"
    return "OK"


def build_detail_rows(result: FileTypeAuditResult) -> list[dict]:
    rows: list[dict] = []
    canonical = result.canonical_columns

    for header in result.headers:
        missing: list[str] = []
        extra: list[str] = []
        order_differs = False
        matches = False

        if header.read_status == "ok" and canonical:
            missing, extra, order_differs = compare_columns_to_canonical(
                header.columns, canonical
            )
            matches = (
                not missing
                and not extra
                and not order_differs
                and not header.duplicate_columns
                and not header.whitespace_columns
            )

        rows.append(
            {
                "file_type": result.file_type,
                "source_folder": header.source_folder,
                "source_year": header.source_year or "",
                "source_quarter": header.source_quarter or "",
                "file_path": header.file_path,
                "column_count": header.column_count,
                "schema_hash": header.schema_hash,
                "matches_canonical": matches,
                "missing_columns": ";".join(missing),
                "extra_columns": ";".join(extra),
                "order_differs": order_differs,
                "duplicate_columns": ";".join(header.duplicate_columns),
                "read_status": header.read_status,
                "error_message": header.error_message or "",
            }
        )
    return rows


def build_summary_row(result: FileTypeAuditResult) -> dict:
    return {
        "file_type": result.file_type,
        "folders_detected": result.folders_detected,
        "unique_schema_versions": result.unique_schema_versions,
        "canonical_column_count": len(result.canonical_columns),
        "folders_with_missing_columns": result.folders_with_missing,
        "folders_with_extra_columns": result.folders_with_extra,
        "folders_with_order_differences": result.folders_with_order_diff,
        "empty_files": result.empty_files,
        "unreadable_files": result.unreadable_files,
        "status": result.status,
    }


def build_canonical_json(results: list[FileTypeAuditResult]) -> dict:
    payload: dict = {}
    for result in results:
        payload[result.file_type] = {
            "canonical_columns": result.canonical_columns,
            "all_detected_columns": result.all_detected_columns,
            "schema_versions": result.schema_versions,
            "casing_variants": result.casing_variants,
            "renamed_column_candidates": result.renamed_candidates,
            "status": result.status,
        }
    return payload


def export_results(
    results: list[FileTypeAuditResult],
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_path = out_dir / "schema_audit_summary.csv"
    details_path = out_dir / "schema_audit_details.csv"
    canonical_path = out_dir / "canonical_columns.json"

    summary_rows = [build_summary_row(result) for result in results]
    detail_rows: list[dict] = []
    for result in results:
        detail_rows.extend(build_detail_rows(result))

    summary_fields = list(summary_rows[0].keys()) if summary_rows else []
    detail_fields = list(detail_rows[0].keys()) if detail_rows else []

    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summary_rows)

    with details_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=detail_fields)
        writer.writeheader()
        writer.writerows(detail_rows)

    write_json(canonical_path, build_canonical_json(results))

    logger.info("wrote %s", summary_path)
    logger.info("wrote %s", details_path)
    logger.info("wrote %s", canonical_path)


def print_console_summary(results: list[FileTypeAuditResult]) -> None:
    print("\nSEC file structure audit complete.\n")

    for result in results:
        print(f"{result.file_type}:")
        print(f"- folders detected: {result.folders_detected}")
        print(f"- unique schema versions: {result.unique_schema_versions}")

        if result.canonical_columns:
            print(f"- canonical column count: {len(result.canonical_columns)}")

        for header in result.headers:
            if header.read_status != "ok":
                continue
            missing, extra, order_differs = compare_columns_to_canonical(
                header.columns, result.canonical_columns
            )
            if missing:
                print(
                    f"- missing columns in {header.source_folder}: "
                    f"{', '.join(missing)}"
                )
            if extra:
                print(
                    f"- extra columns in {header.source_folder}: "
                    f"{', '.join(extra)}"
                )
            if order_differs:
                print(f"- column order differs in {header.source_folder}")
            if header.duplicate_columns:
                print(
                    f"- duplicate columns in {header.source_folder}: "
                    f"{', '.join(header.duplicate_columns)}"
                )
            if header.whitespace_columns:
                print(
                    f"- whitespace in column names ({header.source_folder}): "
                    f"{', '.join(header.whitespace_columns)}"
                )

        for entry in result.renamed_candidates[:5]:
            print(
                f"- possible rename in {entry['source_folder']}: "
                f"{entry['expected_column']} -> {entry['candidate_column']}"
            )
        if len(result.renamed_candidates) > 5:
            print(f"- ... and {len(result.renamed_candidates) - 5} more rename candidates")

        if result.casing_variants:
            sample = next(iter(result.casing_variants.values()))
            print(f"- inconsistent casing variants detected (e.g. {sample})")

        if result.empty_files:
            print(f"- empty files: {result.empty_files}")
        if result.unreadable_files:
            print(f"- unreadable files: {result.unreadable_files}")

        print(f"- status: {result.status}\n")


def run_audit(raw_dir: Path, out_dir: Path) -> list[FileTypeAuditResult]:
    raw_root = raw_dir.resolve()
    if not raw_root.exists():
        raise FileNotFoundError(f"Raw SEC directory not found: {raw_root}")

    grouped = discover_sec_tsv_files(raw_root)
    if not any(paths for paths in grouped.values()):
        raise FileNotFoundError(f"No SEC TSV files found under {raw_root}")

    results: list[FileTypeAuditResult] = []
    for file_type in KNOWN_SEC_FILES:
        results.append(audit_file_type(file_type, grouped[file_type], raw_root))

    export_results(results, out_dir)
    return results


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit SEC TSV header consistency across quarterly folders."
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Root directory containing SEC quarterly folders.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Output directory for audit reports.",
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
        results = run_audit(args.raw_dir, args.out_dir)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.exception("audit failed: %s", exc)
        sys.exit(1)

    print_console_summary(results)
    elapsed = time.perf_counter() - started
    print(f"Reports written to: {args.out_dir.resolve()}")
    print(f"Total runtime: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
