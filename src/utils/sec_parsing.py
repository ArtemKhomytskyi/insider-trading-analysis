"""Parse SEC raw text fields (dates and numbers) without silent coercion."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

_MONTHS: dict[str, int] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_COMPACT_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DMON_DATE_RE = re.compile(r"^(\d{1,2})-([A-Z]{3})-(\d{2,4})$", re.IGNORECASE)
_NUMERIC_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _blank(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip() == ""


def parse_sec_date(value: Any) -> date | None:
    """
    Parse SEC date strings into ``datetime.date``.

    Supports:
    - YYYY-MM-DD
    - YYYYMMDD
    - DD-MON-YYYY (e.g. 15-JAN-2020)
    - DD-MON-YY
    """
    if _blank(value):
        return None

    text = str(value).strip()

    match = _ISO_DATE_RE.match(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    match = _COMPACT_DATE_RE.match(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    match = _DMON_DATE_RE.match(text)
    if match:
        day = int(match.group(1))
        month = _MONTHS.get(match.group(2).upper())
        if month is None:
            return None
        year_raw = int(match.group(3))
        year = year_raw if year_raw > 99 else 2000 + year_raw if year_raw < 70 else 1900 + year_raw
        try:
            return date(year, month, day)
        except ValueError:
            return None

    # Fallback for datetime-like strings
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    return None


def parse_sec_number(value: Any) -> float | None:
    """
    Parse SEC numeric text fields.

    Returns None for blanks, footnote placeholders, and other non-numeric text.
    Preserves zero and negative values when explicitly numeric.
    """
    if _blank(value):
        return None

    text = str(value).strip()
    if not text:
        return None

    if "," in text:
        if not re.match(r"^\d{1,3}(,\d{3})*(\.\d+)?$", text):
            return None
        text = text.replace(",", "")

    if not _NUMERIC_RE.match(text):
        return None

    try:
        return float(text)
    except ValueError:
        return None


def parse_sec_date_series(values) -> list[date | None]:
    return [parse_sec_date(v) for v in values]


def parse_sec_number_series(values) -> list[float | None]:
    return [parse_sec_number(v) for v in values]
