"""Shared SEC raw ingestion utilities."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

KNOWN_SEC_FILES: tuple[str, ...] = (
    "SUBMISSION.tsv",
    "REPORTINGOWNER.tsv",
    "NONDERIV_TRANS.tsv",
    "NONDERIV_HOLDING.tsv",
    "DERIV_TRANS.tsv",
    "DERIV_HOLDING.tsv",
    "FOOTNOTES.tsv",
    "OWNER_SIGNATURE.tsv",
)

FILE_TO_TABLE: dict[str, str] = {
    "SUBMISSION.tsv": "raw_submission",
    "REPORTINGOWNER.tsv": "raw_reporting_owner",
    "NONDERIV_TRANS.tsv": "raw_nonderiv_trans",
    "NONDERIV_HOLDING.tsv": "raw_nonderiv_holding",
    "DERIV_TRANS.tsv": "raw_deriv_trans",
    "DERIV_HOLDING.tsv": "raw_deriv_holding",
    "FOOTNOTES.tsv": "raw_footnotes",
    "OWNER_SIGNATURE.tsv": "raw_owner_signature",
}

RAW_TABLES: tuple[str, ...] = tuple(sorted(set(FILE_TO_TABLE.values())))

METADATA_COLUMNS: tuple[str, ...] = (
    "source_file",
    "source_year",
    "source_quarter",
    "source_folder",
    "ingested_at",
    "row_hash",
)

HASH_SEPARATOR = "\x1f"
SCHEMA_HASH_SEPARATOR = "\x1e"

YEAR_QUARTER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?P<year>\d{4})\s*[_\-\s]?\s*Q\s*(?P<quarter>[1-4])\b", re.IGNORECASE),
    re.compile(r"(?P<year>\d{4})\s*Q\s*(?P<quarter>[1-4])\b", re.IGNORECASE),
    re.compile(r"(?P<year>\d{4})q(?P<quarter>[1-4])", re.IGNORECASE),
    re.compile(r"(?P<year>\d{4})[_\-](?P<quarter>[1-4])(?:_|$|[^\d])", re.IGNORECASE),
)


@dataclass
class TableIngestionStats:
    table_name: str
    inserted: int = 0
    skipped: int = 0


@dataclass
class IngestionSummary:
    table_stats: dict[str, TableIngestionStats] = field(default_factory=dict)
    source_folders: list[str] = field(default_factory=list)
    years_detected: list[int] = field(default_factory=list)
    missing_files_by_folder: dict[str, list[str]] = field(default_factory=dict)

    def get_table_stats(self, table_name: str) -> TableIngestionStats:
        if table_name not in self.table_stats:
            self.table_stats[table_name] = TableIngestionStats(table_name=table_name)
        return self.table_stats[table_name]


@dataclass
class FileHeaderInfo:
    file_type: str
    source_folder: str
    source_year: int | None
    source_quarter: int | None
    file_path: str
    columns: list[str] = field(default_factory=list)
    column_count: int = 0
    schema_hash: str = ""
    duplicate_columns: list[str] = field(default_factory=list)
    whitespace_columns: list[str] = field(default_factory=list)
    read_status: str = "ok"
    error_message: str | None = None


def parse_year_quarter(name: str) -> tuple[int | None, int | None]:
    """Parse year/quarter from folder or path segment names."""
    text = name.strip()
    if not text:
        return None, None

    for pattern in YEAR_QUARTER_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group("year")), int(match.group("quarter"))
    return None, None


def parse_year_quarter_from_path(path: Path) -> tuple[int | None, int | None]:
    for part in reversed(path.parts):
        year, quarter = parse_year_quarter(part)
        if year is not None:
            return year, quarter
    return None, None


def map_file_to_table(filename: str) -> str | None:
    return FILE_TO_TABLE.get(filename)


def is_known_sec_file(filename: str) -> bool:
    return filename in FILE_TO_TABLE


def relative_to_raw(path: Path, raw_root: Path) -> str:
    return path.resolve().relative_to(raw_root.resolve()).as_posix()


def discover_sec_tsv_files(raw_root: Path) -> dict[str, list[Path]]:
    """Return known SEC TSV files grouped by file type."""
    grouped: dict[str, list[Path]] = {name: [] for name in KNOWN_SEC_FILES}
    for file_type in KNOWN_SEC_FILES:
        grouped[file_type] = sorted(raw_root.rglob(file_type), key=lambda p: p.as_posix())
    return grouped


def normalize_column_key(name: str) -> str:
    """Normalize column name for comparison (case/whitespace/underscore insensitive)."""
    return re.sub(r"[\s_]+", "", name).upper()


def compute_schema_hash(columns: list[str]) -> str:
    payload = SCHEMA_HASH_SEPARATOR.join(columns)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def find_duplicate_columns(columns: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for column in columns:
        if column in seen and column not in duplicates:
            duplicates.append(column)
        seen.add(column)
    return duplicates


def find_whitespace_columns(columns: list[str]) -> list[str]:
    return [column for column in columns if column != column.strip()]


def read_tsv_header(path: Path) -> tuple[list[str], str, str | None]:
    """
    Read only the first line (header) of a TSV file.

    Returns (columns, read_status, error_message).
    read_status: ok | empty | unreadable
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            header_line = handle.readline()
    except OSError as exc:
        return [], "unreadable", str(exc)

    if not header_line or not header_line.strip():
        return [], "empty", None

    columns = header_line.rstrip("\n\r").split("\t")
    return columns, "ok", None


def select_canonical_columns(
    headers: list[FileHeaderInfo],
) -> list[str]:
    """Pick the most common column schema among successfully read headers."""
    counts: dict[str, list[str]] = {}
    for header in headers:
        if header.read_status != "ok" or not header.columns:
            continue
        counts.setdefault(header.schema_hash, header.columns)

    if not counts:
        return []

    hash_counts: dict[str, int] = {}
    for header in headers:
        if header.read_status == "ok" and header.schema_hash:
            hash_counts[header.schema_hash] = hash_counts.get(header.schema_hash, 0) + 1

    # Prefer the most common schema; on ties prefer fewer columns (baseline SEC layout).
    best_hash = max(
        hash_counts,
        key=lambda schema_hash: (
            hash_counts[schema_hash],
            -len(counts[schema_hash]),
        ),
    )
    return list(counts[best_hash])


def compare_columns_to_canonical(
    columns: list[str],
    canonical: list[str],
) -> tuple[list[str], list[str], bool]:
    """Return missing columns, extra columns, and whether order differs."""
    canonical_set = set(canonical)
    column_set = set(columns)
    missing = [col for col in canonical if col not in column_set]
    extra = [col for col in columns if col not in canonical_set]
    order_differs = not missing and not extra and columns != canonical
    return missing, extra, order_differs


def suggest_renamed_columns(
    missing_columns: list[str],
    extra_columns: list[str],
) -> dict[str, str]:
    """Map missing canonical names to likely renamed extras in the same file."""
    suggestions: dict[str, str] = {}
    extra_by_key = {normalize_column_key(col): col for col in extra_columns}

    for missing in missing_columns:
        key = normalize_column_key(missing)
        if key in extra_by_key:
            suggestions[missing] = extra_by_key[key]
    return suggestions


def detect_casing_variants(
    headers: list[FileHeaderInfo],
) -> dict[str, list[str]]:
    """Map normalized column keys to distinct raw spellings seen across folders."""
    variants: dict[str, set[str]] = {}
    for header in headers:
        if header.read_status != "ok":
            continue
        for column in header.columns:
            variants.setdefault(normalize_column_key(column), set()).add(column)

    return {
        key: sorted(spellings)
        for key, spellings in variants.items()
        if len(spellings) > 1
    }


def build_schema_versions(
    headers: list[FileHeaderInfo],
) -> dict[str, dict]:
    versions: dict[str, dict] = {}
    for header in headers:
        if header.read_status != "ok":
            continue
        entry = versions.setdefault(
            header.schema_hash,
            {"columns": header.columns, "folders": [], "count": 0},
        )
        entry["folders"].append(header.source_folder)
        entry["count"] += 1
    return versions


def audit_missing_files(source_folders: list[Path]) -> dict[str, list[str]]:
    missing: dict[str, list[str]] = {}
    for folder in source_folders:
        absent = [name for name in KNOWN_SEC_FILES if not (folder / name).exists()]
        if absent:
            missing[folder.name] = absent
    return missing


def compute_row_hash(
    row_values: list[str],
    source_file: str,
    source_folder: str,
) -> str:
    payload = HASH_SEPARATOR.join(row_values)
    digest_input = f"{source_file}{HASH_SEPARATOR}{source_folder}{HASH_SEPARATOR}{payload}"
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def add_row_hashes(
    df: pd.DataFrame,
    data_columns: list[str],
    source_file: str,
    source_folder: str,
) -> pd.Series:
    values = df[data_columns].astype(str)
    joined = values.agg(HASH_SEPARATOR.join, axis=1)
    prefix = f"{source_file}{HASH_SEPARATOR}{source_folder}{HASH_SEPARATOR}"
    return (prefix + joined).map(
        lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    )


def align_chunk_to_table(
    chunk: pd.DataFrame,
    table_columns: list[str],
) -> pd.DataFrame:
    data_columns = [column for column in table_columns if column not in METADATA_COLUMNS]
    aligned = pd.DataFrame(index=chunk.index)

    for column in data_columns:
        if column in chunk.columns:
            aligned[column] = chunk[column].astype(str)
        else:
            aligned[column] = ""

    return aligned, data_columns


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
