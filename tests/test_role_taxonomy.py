"""Tests for insider role taxonomy utilities."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.utils.role_taxonomy import (
    classify_role,
    get_role_weight,
    is_board_role,
    is_executive_role,
    load_role_taxonomy,
    normalize_role_title,
    parse_relationship_flags,
)


def test_normalize_role_title() -> None:
    assert normalize_role_title("  Chief Executive Officer  ") == "CHIEF EXECUTIVE OFFICER"


def test_ceo_variants() -> None:
    for title in ("CEO", "Chief Executive Officer", "President and CEO"):
        meta = classify_role(title)
        assert meta["normalized_role"] == "CEO"
        assert meta["role_weight"] == 1.0
        assert meta["is_executive"] is True


def test_cfo_variants() -> None:
    for title in ("CFO", "EVP & CFO"):
        meta = classify_role(title)
        assert meta["normalized_role"] == "CFO"
        assert meta["role_weight"] == 0.9


def test_coo() -> None:
    assert classify_role("Chief Operating Officer")["normalized_role"] == "COO"


def test_director_and_chairman() -> None:
    assert classify_role("Director")["normalized_role"] == "DIRECTOR"
    assert classify_role("Chairman of the Board")["normalized_role"] == "CHAIRMAN"
    assert is_board_role("Director")


def test_legal_medical_tech() -> None:
    assert classify_role("General Counsel")["normalized_role"] == "LEGAL"
    assert classify_role("Chief Medical Officer")["normalized_role"] == "MEDICAL"
    assert classify_role("Chief Technology Officer")["normalized_role"] == "TECHNOLOGY"


def test_large_owner() -> None:
    meta = classify_role("10% Owner")
    assert meta["normalized_role"] == "LARGE_OWNER_10PCT"
    assert meta["is_large_owner"] is True


def test_unknown_title() -> None:
    meta = classify_role("See Remarks")
    assert meta["normalized_role"] == "UNKNOWN"
    assert meta["requires_review"] is True
    assert get_role_weight("See Remarks") == 0.2


def test_relationship_flags() -> None:
    flags = parse_relationship_flags("Director,Officer,TenPercentOwner")
    assert flags["is_director"] is True
    assert flags["is_officer"] is True
    assert flags["is_ten_percent_owner"] is True


def test_relationship_fallback_owner() -> None:
    meta = classify_role("", relationship_text="TenPercentOwner")
    assert meta["normalized_role"] == "LARGE_OWNER_10PCT"
    assert meta["is_large_owner"] is True


def test_is_executive_role() -> None:
    assert is_executive_role("CEO")
    assert not is_executive_role("Director")


def test_custom_map(tmp_path: Path) -> None:
    custom = tmp_path / "roles.csv"
    pd.DataFrame(
        [
            {
                "raw_pattern": "CUSTOM ROLE",
                "normalized_role": "EXECUTIVE_OTHER",
                "role_group": "EXECUTIVE",
                "role_seniority": "EXECUTIVE",
                "role_weight": "0.55",
                "is_executive": "true",
                "is_board_member": "false",
                "is_large_owner": "false",
                "include_clean_mvp": "true",
                "requires_review": "false",
                "confidence": "HIGH",
                "economic_interpretation": "Test",
                "notes": "",
            }
        ]
    ).to_csv(custom, index=False)

    from src.utils.role_taxonomy import clear_role_taxonomy_cache

    clear_role_taxonomy_cache()
    meta = classify_role("Custom Role", map_path=custom)
    clear_role_taxonomy_cache()
    assert meta["normalized_role"] == "EXECUTIVE_OTHER"
