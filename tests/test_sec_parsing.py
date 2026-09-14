"""Tests for SEC date and number parsing."""

from __future__ import annotations

from datetime import date

import pytest

from src.utils.sec_parsing import parse_sec_date, parse_sec_number


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2020-01-15", date(2020, 1, 15)),
        ("20200115", date(2020, 1, 15)),
        ("15-JAN-2020", date(2020, 1, 15)),
        ("01-APR-2008", date(2008, 4, 1)),
        ("29-MAR-2006", date(2006, 3, 29)),
        ("  15-JAN-2020  ", date(2020, 1, 15)),
        ("15-JAN-20", date(2020, 1, 15)),
    ],
)
def test_parse_sec_date_supported_formats(raw: str, expected: date) -> None:
    assert parse_sec_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", None, "   ", "N/A", "SEE FOOTNOTE", "15-XXX-2020", "not-a-date"],
)
def test_parse_sec_date_returns_none_for_invalid(raw) -> None:
    assert parse_sec_date(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1000", 1000.0),
        ("1,234.56", 1234.56),
        ("0", 0.0),
        ("-12.5", -12.5),
        ("  42.0  ", 42.0),
        ("1e3", 1000.0),
    ],
)
def test_parse_sec_number_supported_formats(raw: str, expected: float) -> None:
    assert parse_sec_number(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", None, "   ", "SEE FN 3", "1.2.3", "12,34,56", "--", "abc"],
)
def test_parse_sec_number_returns_none_for_non_numeric(raw) -> None:
    assert parse_sec_number(raw) is None
