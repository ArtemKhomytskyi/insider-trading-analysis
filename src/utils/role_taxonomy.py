"""Insider role/title taxonomy utilities (config-driven)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.db import PROJECT_ROOT

DEFAULT_MAP_PATH = PROJECT_ROOT / "configs" / "normalization" / "officer_title_map.csv"

BOOL_COLUMNS = (
    "is_executive",
    "is_board_member",
    "is_large_owner",
    "include_clean_mvp",
    "requires_review",
)
UNKNOWN_ROLE = "UNKNOWN"
MIN_CONTAINS_PATTERN_LEN = 4

RELATIONSHIP_FLAG_MAP = {
    "DIRECTOR": "is_director",
    "OFFICER": "is_officer",
    "TENPERCENTOWNER": "is_ten_percent_owner",
    "10PERCENTOWNER": "is_ten_percent_owner",
    "OTHER": "is_other",
}


def normalize_role_title(raw_title: str | None) -> str:
    if raw_title is None:
        return ""
    text = str(raw_title).upper().strip()
    if not text:
        return ""
    text = re.sub(r"[\(\)\[\]\{\}/]", " ", text)
    text = re.sub(r"[^\w\s\-&%]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_relationship_flags(relationship_text: str | None) -> dict[str, bool]:
    """Parse comma-separated SEC relationship flags."""
    flags = {
        "is_director": False,
        "is_officer": False,
        "is_ten_percent_owner": False,
        "is_other": False,
        "raw_relationship": relationship_text if relationship_text is not None else "",
    }
    if not relationship_text or not str(relationship_text).strip():
        return flags

    parts = re.split(r"[,;/|]+", str(relationship_text).upper())
    for part in parts:
        token = re.sub(r"[^A-Z0-9%]", "", part.strip())
        if not token:
            continue
        key = RELATIONSHIP_FLAG_MAP.get(token)
        if key:
            flags[key] = True
    return flags


@lru_cache(maxsize=4)
def load_role_taxonomy(map_path: str | None = None) -> pd.DataFrame:
    path = Path(map_path) if map_path else DEFAULT_MAP_PATH
    if not path.exists():
        raise FileNotFoundError(f"Role taxonomy map not found: {path}")

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

    if "role_weight" in df.columns:
        df["role_weight"] = pd.to_numeric(df["role_weight"], errors="coerce").fillna(0.2)

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


def _unknown_classification(
    raw_title: str | None,
    normalized_title: str,
    relationship_flags: dict[str, bool],
) -> dict[str, Any]:
    is_board = relationship_flags.get("is_director", False)
    is_owner = relationship_flags.get("is_ten_percent_owner", False)
    normalized_role = UNKNOWN_ROLE
    role_weight = 0.2
    if is_owner:
        normalized_role = "LARGE_OWNER_10PCT"
        role_weight = 0.6
    return {
        "raw_role_title": raw_title if raw_title is not None else "",
        "normalized_title": normalized_title,
        "normalized_role": normalized_role,
        "role_group": "OWNER" if is_owner else "UNKNOWN",
        "role_seniority": "UNKNOWN",
        "role_weight": role_weight,
        "is_executive": relationship_flags.get("is_officer", False),
        "is_board_member": is_board,
        "is_large_owner": is_owner,
        "include_clean_mvp": normalized_role != UNKNOWN_ROLE,
        "requires_review": True,
        "confidence": "LOW",
        "matched_pattern": "",
        "economic_interpretation": "Unclassified role title",
        "notes": "No title pattern match; relationship flags applied if present",
        "relationship_flags": relationship_flags,
    }


def classify_role(
    raw_title: str | None,
    relationship_text: str | None = None,
    mapping_df: pd.DataFrame | None = None,
    map_path: Path | None = None,
) -> dict[str, Any]:
    """Classify insider role from title (and optional relationship text)."""
    normalized = normalize_role_title(raw_title)
    relationship_flags = parse_relationship_flags(relationship_text)

    if not normalized and not any(
        relationship_flags.get(k)
        for k in ("is_director", "is_officer", "is_ten_percent_owner", "is_other")
    ):
        result = _unknown_classification(raw_title, normalized, relationship_flags)
        result["notes"] = "Blank title and relationship"
        return result

    df = mapping_df if mapping_df is not None else load_role_taxonomy(
        _resolve_map_path(map_path)
    )

    for _, row in df.iterrows():
        pattern = str(row["raw_pattern"]).upper()
        if not _pattern_matches(normalized, pattern):
            continue
        is_exec = bool(row.get("is_executive", False)) or relationship_flags.get(
            "is_officer", False
        )
        is_board = bool(row.get("is_board_member", False)) or relationship_flags.get(
            "is_director", False
        )
        is_owner = bool(row.get("is_large_owner", False)) or relationship_flags.get(
            "is_ten_percent_owner", False
        )
        return {
            "raw_role_title": raw_title if raw_title is not None else "",
            "normalized_title": normalized,
            "normalized_role": row.get("normalized_role", UNKNOWN_ROLE),
            "role_group": row.get("role_group", "UNKNOWN"),
            "role_seniority": row.get("role_seniority", "UNKNOWN"),
            "role_weight": float(row.get("role_weight", 0.2)),
            "is_executive": is_exec,
            "is_board_member": is_board,
            "is_large_owner": is_owner,
            "include_clean_mvp": bool(row.get("include_clean_mvp", False)),
            "requires_review": bool(row.get("requires_review", False)),
            "confidence": str(row.get("confidence", "MEDIUM")),
            "matched_pattern": pattern,
            "economic_interpretation": row.get("economic_interpretation", ""),
            "notes": row.get("notes", ""),
            "relationship_flags": relationship_flags,
        }

    return _unknown_classification(raw_title, normalized, relationship_flags)


def is_executive_role(
    raw_title: str | None,
    relationship_text: str | None = None,
    map_path: Path | None = None,
) -> bool:
    return bool(classify_role(raw_title, relationship_text, map_path=map_path).get("is_executive"))


def is_board_role(
    raw_title: str | None,
    relationship_text: str | None = None,
    map_path: Path | None = None,
) -> bool:
    return bool(
        classify_role(raw_title, relationship_text, map_path=map_path).get("is_board_member")
    )


def get_role_weight(
    raw_title: str | None,
    relationship_text: str | None = None,
    map_path: Path | None = None,
) -> float:
    return float(
        classify_role(raw_title, relationship_text, map_path=map_path).get("role_weight", 0.2)
    )


def clear_role_taxonomy_cache() -> None:
    load_role_taxonomy.cache_clear()
