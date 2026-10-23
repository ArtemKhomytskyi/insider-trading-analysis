"""Provider-independent Phase 6 event engineering and return calculations."""

from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HORIZONS = (1, 5, 20, 60, 90, 180)
PRIMARY_POLICY = "NEXT_SESSION_AFTER_FILING_DATE_CLOSE"
ACCEPTANCE_POLICY = "NEXT_FULL_SESSION_AFTER_ACCEPTANCE_DATE_CLOSE"


def stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def easter(year: int) -> date:
    """Gregorian Easter (Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


class UsEquityCalendar:
    """Explicit NYSE regular-session calendar for the pilot era.

    It models regular holidays, known full-day exceptional closures, and the
    common 13:00 ET early closes needed for timing classification. It is not a
    substitute for an exchange-licensed calendar outside the documented range.
    """

    exceptional_closures = {
        date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),
        date(2004, 6, 11), date(2007, 1, 2), date(2012, 10, 29), date(2012, 10, 30),
        date(2018, 12, 5),
    }

    def holidays(self, year: int) -> set[date]:
        days = {
            observed(date(year, 1, 1)),
            nth_weekday(year, 1, 0, 3),
            nth_weekday(year, 2, 0, 3),
            easter(year) - timedelta(days=2),
            last_weekday(year, 5, 0),
            observed(date(year, 7, 4)),
            nth_weekday(year, 9, 0, 1),
            nth_weekday(year, 11, 3, 4),
            observed(date(year, 12, 25)),
        }
        if year >= 2022:
            days.add(observed(date(year, 6, 19)))
        return days | {day for day in self.exceptional_closures if day.year == year}

    def is_session(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays(day.year)

    def next_session(self, day: date, *, strictly_after: bool = True) -> date:
        current = day + timedelta(days=1 if strictly_after else 0)
        while not self.is_session(current):
            current += timedelta(days=1)
        return current

    def sessions(self, start: date, end: date) -> list[date]:
        values = []
        current = start
        while current <= end:
            if self.is_session(current):
                values.append(current)
            current += timedelta(days=1)
        return values

    def early_close(self, day: date) -> bool:
        if not self.is_session(day):
            return False
        thanksgiving = nth_weekday(day.year, 11, 3, 4)
        if day == thanksgiving + timedelta(days=1):
            return True
        if day.month == 7 and day.day == 3 and day.weekday() < 5:
            return True
        if day.month == 12 and day.day == 24 and day.weekday() < 5:
            return True
        return False

    def close_time(self, day: date) -> time:
        return time(13, 0) if self.early_close(day) else time(16, 0)


def sec_business_holidays(year: int) -> set[date]:
    days = {
        observed(date(year, 1, 1)), nth_weekday(year, 1, 0, 3), nth_weekday(year, 2, 0, 3),
        last_weekday(year, 5, 0), observed(date(year, 7, 4)), nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 10, 0, 2), observed(date(year, 11, 11)),
        nth_weekday(year, 11, 3, 4), observed(date(year, 12, 25)),
    }
    if year >= 2021:
        days.add(observed(date(year, 6, 19)))
    return days


def business_days_between(start: date, end: date) -> int:
    """Signed SEC/federal-business-day count, excluding transaction date."""
    if start == end:
        return 0
    sign = 1 if end > start else -1
    lo, hi = (start, end) if sign == 1 else (end, start)
    count = sum(day.weekday() < 5 and day not in sec_business_holidays(day.year)
                for day in (lo + timedelta(days=i) for i in range(1, (hi - lo).days + 1)))
    return sign * count


def classify_acceptance(value: str, cal: UsEquityCalendar) -> str:
    if not value:
        return "UNKNOWN"
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("Acceptance timestamp must be timezone-aware")
    local = stamp.astimezone(ZoneInfo("America/New_York"))
    if not cal.is_session(local.date()):
        return "NON_TRADING_DAY"
    if local.time() < time(9, 30):
        return "BEFORE_MARKET_OPEN"
    if local.time() <= cal.close_time(local.date()):
        return "DURING_REGULAR_HOURS"
    return "AFTER_MARKET_CLOSE"


def lag_bucket(value: int | float | None) -> str:
    if value is None or pd.isna(value):
        return "MISSING"
    value = int(value)
    if value < 0: return "NEGATIVE"
    if value == 0: return "0"
    if value == 1: return "1"
    if value == 2: return "2"
    if value <= 5: return "3-5"
    if value <= 10: return "6-10"
    return ">10"


def add_lag_fields(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    tx = pd.to_datetime(result["transaction_date"], errors="coerce")
    filing = pd.to_datetime(result["filing_date"], errors="coerce")
    result["calendar_days_to_disclose"] = (filing - tx).dt.days
    result["business_days_to_disclose"] = [
        business_days_between(a.date(), b.date()) if pd.notna(a) and pd.notna(b) else np.nan
        for a, b in zip(tx, filing)
    ]
    result["filing_lag_bucket_phase6"] = result["business_days_to_disclose"].map(lag_bucket)
    result["lag_quality_code"] = np.select(
        [tx.isna(), filing.isna(), result["calendar_days_to_disclose"] < 0,
         result["calendar_days_to_disclose"] > 30],
        ["MISSING_TRANSACTION_DATE", "MISSING_FILING_DATE", "NEGATIVE_LAG", "LONG_LAG_GT_30_CALENDAR_DAYS"],
        default="OK",
    )
    return result


def construct_filing_events(transactions: pd.DataFrame, filing_metadata: pd.DataFrame,
                            cal: UsEquityCalendar | None = None) -> pd.DataFrame:
    cal = cal or UsEquityCalendar()
    grouped = transactions.groupby("accession_number", sort=True, dropna=False).agg(
        issuer_cik=("issuer_cik", "first"), ticker=("ticker_clean", "first"),
        filing_date=("filing_date", "first"), transaction_count=("transaction_id", "nunique"),
        unique_insiders=("insider_cik", "nunique"), purchase_count=("is_purchase", "sum"),
        sale_count=("is_sale", "sum"), gross_purchase_value=("purchase_value", "sum"),
        gross_sale_value=("sale_value", "sum"), net_reported_transaction_value=("signed_value", "sum"),
        amendment=("is_amendment", "max"),
    ).reset_index()
    meta = filing_metadata[["accession_number", "form_type", "acceptance_datetime_utc",
                            "acceptance_datetime_new_york", "verification_status"]]
    result = grouped.merge(meta, on="accession_number", how="left", validate="one_to_one")
    result["event_id"] = result["accession_number"].map(lambda value: stable_id("filing", value))
    result["event_aggregation_level"] = "FILING"
    result["event_timing_policy"] = PRIMARY_POLICY
    result["event_session"] = result["filing_date"]
    result["entry_session"] = result["filing_date"].map(
        lambda value: cal.next_session(date.fromisoformat(value)).isoformat())
    result["entry_price_date"] = result["entry_session"]
    result["entry_price_type"] = "ADJUSTED_CLOSE"
    result["acceptance_timing_class"] = result["acceptance_datetime_new_york"].map(
        lambda value: classify_acceptance(value, cal))
    result["acceptance_entry_session"] = result["acceptance_datetime_new_york"].map(
        lambda value: cal.next_session(datetime.fromisoformat(value).date()).isoformat())
    result["timing_policy_differs"] = result["entry_session"] != result["acceptance_entry_session"]
    result["direction"] = np.select(
        [result["gross_purchase_value"] > result["gross_sale_value"],
         result["gross_sale_value"] > result["gross_purchase_value"]],
        ["NET_PURCHASE", "NET_SALE"], default="MIXED_OR_ZERO")
    result["source_provenance"] = "verified SEC transaction layer + verified raw EDGAR filing metadata"
    return result


def aggregate_issuer_sessions(filings: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (issuer, session), group in filings.groupby(["issuer_cik", "entry_session"], sort=True):
        accessions = sorted(group["accession_number"].tolist())
        rows.append({
            "event_id": stable_id("issuer-session", issuer, session), "issuer_cik": issuer,
            "ticker": group["ticker"].iloc[0], "entry_session": session,
            "event_aggregation_level": "ISSUER_SESSION", "filing_count": len(group),
            "accession_numbers": "|".join(accessions), "unique_insiders": int(group["unique_insiders"].sum()),
            "transaction_count": int(group["transaction_count"].sum()),
            "purchase_count": int(group["purchase_count"].sum()), "sale_count": int(group["sale_count"].sum()),
            "gross_purchase_value": float(group["gross_purchase_value"].sum()),
            "gross_sale_value": float(group["gross_sale_value"].sum()),
            "net_reported_transaction_value": float(group["net_reported_transaction_value"].sum()),
        })
    return pd.DataFrame(rows)


def validate_price_series(prices: pd.DataFrame) -> None:
    required = {"identifier", "trading_date", "adjusted_close"}
    if not required <= set(prices.columns):
        raise ValueError(f"Missing price columns: {sorted(required - set(prices.columns))}")
    if prices.duplicated(["identifier", "trading_date"]).any():
        raise ValueError("Duplicate daily bars")
    values = pd.to_numeric(prices["adjusted_close"], errors="coerce")
    if values.isna().any() or (values <= 0).any():
        raise ValueError("Adjusted prices must be finite and positive")


def forward_returns(events: pd.DataFrame, prices: pd.DataFrame,
                    horizons: tuple[int, ...] = HORIZONS) -> pd.DataFrame:
    validate_price_series(prices)
    result = events.copy()
    by_identifier = {key: group.sort_values("trading_date").reset_index(drop=True)
                     for key, group in prices.groupby("identifier")}
    for horizon in horizons:
        result[f"forward_return_{horizon}d"] = np.nan
        result[f"return_status_{horizon}d"] = "MISSING_IDENTIFIER"
    for index, event in result.iterrows():
        series = by_identifier.get(event["identifier"])
        if series is None:
            continue
        positions = series.index[series["trading_date"] == event["entry_session"]].tolist()
        if not positions:
            for horizon in horizons: result.at[index, f"return_status_{horizon}d"] = "MISSING_ENTRY_PRICE"
            continue
        position = positions[0]
        entry = float(series.at[position, "adjusted_close"])
        result.at[index, "entry_adjusted_close"] = entry
        for horizon in horizons:
            if position + horizon >= len(series):
                result.at[index, f"return_status_{horizon}d"] = "RIGHT_CENSORED"
            else:
                exit_price = float(series.at[position + horizon, "adjusted_close"])
                result.at[index, f"forward_return_{horizon}d"] = exit_price / entry - 1
                result.at[index, f"return_status_{horizon}d"] = "COMPLETE"
    return result


def benchmark_adjusted(stock: pd.DataFrame, benchmark: pd.DataFrame,
                       horizons: tuple[int, ...] = HORIZONS) -> pd.DataFrame:
    benchmark_events = stock[["event_id", "entry_session"]].copy()
    benchmark_events["identifier"] = stock["benchmark_identifier"].values
    bench_returns = forward_returns(benchmark_events, benchmark, horizons)
    result = stock.copy()
    for horizon in horizons:
        value = f"forward_return_{horizon}d"
        if value not in result:
            raise ValueError(f"Stock return missing for {horizon} sessions")
        result[f"benchmark_return_{horizon}d"] = bench_returns[value]
        result[f"alpha_vs_benchmark_{horizon}d"] = result[value] - bench_returns[value]
        mismatch = result[f"return_status_{horizon}d"] != bench_returns[f"return_status_{horizon}d"]
        result[f"benchmark_alignment_{horizon}d"] = np.where(mismatch, "UNAVAILABLE_OR_MISMATCH", "ALIGNED")
    return result


def flag_overlaps(events: pd.DataFrame, horizon: int, cal: UsEquityCalendar | None = None) -> pd.Series:
    cal = cal or UsEquityCalendar()
    flagged = pd.Series(False, index=events.index)
    for _, group in events.groupby("issuer_cik"):
        ordered = group.sort_values("entry_session")
        prior_end: date | None = None
        prior_index = None
        for index, row in ordered.iterrows():
            start = date.fromisoformat(row["entry_session"])
            sessions = cal.sessions(start, start + timedelta(days=max(horizon * 3, 10)))
            end = sessions[horizon] if len(sessions) > horizon else sessions[-1]
            if prior_end is not None and start <= prior_end:
                flagged.at[index] = True
                if prior_index is not None: flagged.at[prior_index] = True
            if prior_end is None or end > prior_end:
                prior_end, prior_index = end, index
    return flagged
