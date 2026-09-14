"""SEC transaction code taxonomy utilities (config-driven)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.db import PROJECT_ROOT

DEFAULT_TAXONOMY_PATH = (
    PROJECT_ROOT / "configs" / "normalization" / "transaction_code_map.csv"
)

BOOL_COLUMNS = ("include_clean_mvp", "requires_special_handling")
HIGH_SIGNAL_QUALITIES = frozenset({"HIGH", "MEDIUM"})
OPEN_MARKET_CATEGORY = "OPEN_MARKET"


@lru_cache(maxsize=4)
def load_transaction_code_map(map_path: str | None = None) -> pd.DataFrame:
    """Load transaction code taxonomy from CSV config."""
    path = Path(map_path) if map_path else DEFAULT_TAXONOMY_PATH
    if not path.exists():
        raise FileNotFoundError(f"Transaction code map not found: {path}")

    df = pd.read_csv(path, dtype=str).fillna("")
    df["transaction_code"] = df["transaction_code"].str.strip().str.upper()

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


def _code_index(map_path: Path | None = None) -> dict[str, dict[str, Any]]:
    df = load_transaction_code_map(_resolve_map_path(map_path))
    records = df.set_index("transaction_code", drop=False).to_dict(orient="index")
    return {str(code).upper(): meta for code, meta in records.items()}


def get_transaction_code_metadata(
    code: str | None,
    map_path: Path | None = None,
) -> dict[str, Any] | None:
    """Return taxonomy metadata for a transaction code, or None if unknown."""
    if code is None or str(code).strip() == "":
        return None
    return _code_index(map_path).get(str(code).strip().upper())


def is_open_market_code(code: str | None, map_path: Path | None = None) -> bool:
    meta = get_transaction_code_metadata(code, map_path)
    return bool(meta and meta.get("category") == OPEN_MARKET_CATEGORY)


def is_high_signal_code(code: str | None, map_path: Path | None = None) -> bool:
    """True for taxonomy rows with HIGH or MEDIUM signal quality."""
    meta = get_transaction_code_metadata(code, map_path)
    if not meta:
        return False
    return str(meta.get("signal_quality", "")).upper() in HIGH_SIGNAL_QUALITIES


def is_clean_mvp_code(code: str | None, map_path: Path | None = None) -> bool:
    meta = get_transaction_code_metadata(code, map_path)
    return bool(meta and meta.get("include_clean_mvp"))


def clear_taxonomy_cache() -> None:
    load_transaction_code_map.cache_clear()
