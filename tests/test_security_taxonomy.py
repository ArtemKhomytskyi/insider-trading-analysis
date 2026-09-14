"""Tests for security title taxonomy utilities."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.utils.security_taxonomy import (
    classify_security_title,
    is_common_equity,
    load_security_title_map,
    normalize_security_title,
)


def test_normalize_security_title() -> None:
    assert normalize_security_title("  Class A Common Stock  ") == "CLASS A COMMON STOCK"
    assert normalize_security_title("Stock Option (Right to Buy)") == "STOCK OPTION RIGHT TO BUY"


def test_common_equity_titles() -> None:
    for title in (
        "Common Stock",
        "COMMON STOCK",
        "Class A Common Stock",
        "Ordinary Shares",
    ):
        meta = classify_security_title(title)
        assert meta["normalized_security_type"] == "COMMON_EQUITY"
        assert meta["include_clean_mvp"] is True
        assert is_common_equity(title)


def test_stock_compensation() -> None:
    meta = classify_security_title("Restricted Stock Units")
    assert meta["normalized_security_type"] == "STOCK_COMPENSATION"
    assert meta["include_clean_mvp"] is False


def test_option_derivative() -> None:
    meta = classify_security_title("Stock Option (Right to Buy)")
    assert meta["normalized_security_type"] == "OPTION_DERIVATIVE"
    assert meta["include_clean_mvp"] is False


def test_preferred_equity() -> None:
    meta = classify_security_title("Preferred Stock")
    assert meta["normalized_security_type"] == "PREFERRED_EQUITY"
    assert meta["include_clean_mvp"] is False


def test_phantom_deferred() -> None:
    meta = classify_security_title("Phantom Stock Units")
    assert meta["normalized_security_type"] == "PHANTOM_OR_DEFERRED"
    assert meta["include_clean_mvp"] is False


def test_common_units_requires_review() -> None:
    meta = classify_security_title("Common Units")
    assert meta["normalized_security_type"] == "COMMON_UNITS_OR_PARTNERSHIP"
    assert meta["include_clean_mvp"] is False
    assert meta["requires_review"] is True


def test_unknown_weird_title() -> None:
    meta = classify_security_title("Totally Made Up Security XYZ")
    assert meta["normalized_security_type"] in {"UNKNOWN", "OTHER"}
    assert meta["include_clean_mvp"] is False


def test_pattern_order_rsu_before_common() -> None:
    meta = classify_security_title("Restricted Stock Units tied to Common Stock plan")
    assert meta["normalized_security_type"] == "STOCK_COMPENSATION"


def test_custom_map_path(tmp_path: Path) -> None:
    custom = tmp_path / "map.csv"
    pd.DataFrame(
        [
            {
                "raw_pattern": "CUSTOM SECURITY",
                "normalized_security_type": "OTHER",
                "include_clean_mvp": "false",
                "confidence": "LOW",
                "requires_review": "true",
                "economic_interpretation": "Test",
                "notes": "",
            }
        ]
    ).to_csv(custom, index=False)

    from src.utils.security_taxonomy import clear_security_taxonomy_cache

    clear_security_taxonomy_cache()
    meta = classify_security_title("Custom Security", map_path=custom)
    clear_security_taxonomy_cache()
    assert meta["matched_pattern"] == "CUSTOM SECURITY"
