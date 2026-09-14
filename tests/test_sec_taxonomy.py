"""Tests for SEC transaction code taxonomy utilities."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.utils.sec_taxonomy import (
    get_transaction_code_metadata,
    is_clean_mvp_code,
    is_high_signal_code,
    is_open_market_code,
    load_transaction_code_map,
)


def test_load_transaction_code_map_has_required_codes() -> None:
    df = load_transaction_code_map()
    codes = set(df["transaction_code"])
    for code in ("P", "S", "A", "M", "F"):
        assert code in codes


def test_open_market_codes() -> None:
    assert is_open_market_code("P")
    assert is_open_market_code("S")
    assert not is_open_market_code("A")


def test_high_signal_codes() -> None:
    assert is_high_signal_code("P")
    assert is_high_signal_code("S")
    assert not is_high_signal_code("F")


def test_clean_mvp_codes() -> None:
    assert is_clean_mvp_code("P")
    assert is_clean_mvp_code("S")
    assert not is_clean_mvp_code("M")


def test_get_metadata_unknown_code() -> None:
    assert get_transaction_code_metadata("ZZ") is None


def test_get_metadata_fields() -> None:
    meta = get_transaction_code_metadata("P")
    assert meta is not None
    assert meta["category"] == "OPEN_MARKET"
    assert meta["signal_quality"] == "HIGH"
    assert meta["include_clean_mvp"] is True


def test_custom_map_path(tmp_path: Path) -> None:
    custom = tmp_path / "map.csv"
    pd.DataFrame(
        [
            {
                "transaction_code": "Z",
                "meaning": "Custom",
                "category": "OTHER",
                "signal_direction": "NONE",
                "signal_quality": "LOW",
                "include_clean_mvp": "false",
                "requires_special_handling": "true",
                "economic_interpretation": "Test",
                "notes": "",
            }
        ]
    ).to_csv(custom, index=False)

    from src.utils.sec_taxonomy import clear_taxonomy_cache

    clear_taxonomy_cache()
    meta = get_transaction_code_metadata("Z", map_path=custom)
    clear_taxonomy_cache()
    assert meta is not None
    assert meta["meaning"] == "Custom"
