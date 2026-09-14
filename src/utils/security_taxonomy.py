"""Security title taxonomy utilities (config-driven pattern matching)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.db import PROJECT_ROOT

DEFAULT_MAP_PATH = PROJECT_ROOT / "configs" / "normalization" / "security_title_map.csv"

BOOL_COLUMNS = ("include_clean_mvp", "requires_review")
UNKNOWN_TYPE = "UNKNOWN"
OTHER_TYPE = "OTHER"
MIN_CONTAINS_PATTERN_LEN = 4


def normalize_security_title(raw_title: str | None) -> str:
    """
    Normalize raw SECURITY_TITLE for pattern matching.

    - uppercase
    - strip whitespace
    - collapse multiple spaces
    - replace parentheses/brackets with spaces
  """
    if raw_title is None:
        return ""
    text = str(raw_title).upper().strip()
    if not text:
        return ""
    text = re.sub(r"[\(\)\[\]\{\}/]", " ", text)
    text = re.sub(r"[^\w\s\-&]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


@lru_cache(maxsize=4)
def load_security_title_map(map_path: str | None = None) -> pd.DataFrame:
    path = Path(map_path) if map_path else DEFAULT_MAP_PATH
    if not path.exists():
        raise FileNotFoundError(f"Security title map not found: {path}")

    df = pd.read_csv(path, dtype=str).fillna("")
    df["raw_pattern"] = df["raw_pattern"].str.strip().str.upper()

    for column in BOOL_COLUMNS:
        if column in df.columns:
            df[column] = (
                df[column]
                .astype(str)
                .str.strip()
                .str.lower()
                .map({"true": True, "false": False, "1": True, "0": False})
                .fillna(False)
            )
    return df


def _resolve_map_path(map_path: Path | None) -> str | None:
    if map_path is None:
        return None
    return str(map_path.resolve())


def _pattern_matches(normalized_title: str, pattern: str) -> bool:
    if not normalized_title or not pattern:
        return False
    if normalized_title == pattern:
        return True
    if len(pattern) < MIN_CONTAINS_PATTERN_LEN:
        return False
    return pattern in normalized_title


def _unknown_classification(raw_title: str | None, normalized_title: str) -> dict[str, Any]:
    return {
        "raw_title": raw_title if raw_title is not None else "",
        "normalized_title": normalized_title,
        "normalized_security_type": UNKNOWN_TYPE,
        "include_clean_mvp": False,
        "confidence": "LOW",
        "requires_review": True,
        "matched_pattern": "",
        "economic_interpretation": "Unclassified security title",
        "notes": "No pattern match in security_title_map.csv",
    }


def classify_security_title(
    raw_title: str | None,
    mapping_df: pd.DataFrame | None = None,
    map_path: Path | None = None,
) -> dict[str, Any]:
    """
    Classify a security title using ordered CSV patterns (first match wins).

    Returns metadata including raw_title, normalized_title, type, and match info.
    """
    normalized = normalize_security_title(raw_title)
    if not normalized:
        result = _unknown_classification(raw_title, normalized)
        result["normalized_security_type"] = OTHER_TYPE
        result["notes"] = "Blank security title"
        return result

    df = mapping_df if mapping_df is not None else load_security_title_map(
        _resolve_map_path(map_path)
    )

    for _, row in df.iterrows():
        pattern = str(row["raw_pattern"]).upper()
        if not _pattern_matches(normalized, pattern):
            continue
        return {
            "raw_title": raw_title if raw_title is not None else "",
            "normalized_title": normalized,
            "normalized_security_type": row.get("normalized_security_type", UNKNOWN_TYPE),
            "include_clean_mvp": bool(row.get("include_clean_mvp", False)),
            "confidence": str(row.get("confidence", "MEDIUM")),
            "requires_review": bool(row.get("requires_review", False)),
            "matched_pattern": pattern,
            "economic_interpretation": row.get("economic_interpretation", ""),
            "notes": row.get("notes", ""),
        }

    return _unknown_classification(raw_title, normalized)


def is_common_equity(
    raw_title: str | None,
    map_path: Path | None = None,
) -> bool:
    meta = classify_security_title(raw_title, map_path=map_path)
    return (
        meta.get("normalized_security_type") == "COMMON_EQUITY"
        and meta.get("include_clean_mvp") is True
    )


def clear_security_taxonomy_cache() -> None:
    load_security_title_map.cache_clear()
