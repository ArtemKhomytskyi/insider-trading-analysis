"""Deterministic offline tests for Phase 6 event and return engineering."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.market_data.phase6_engine import (
    UsEquityCalendar, add_lag_fields, aggregate_issuer_sessions,
    benchmark_adjusted, classify_acceptance, construct_filing_events,
    flag_overlaps, forward_returns, stable_id, validate_price_series,
)
from src.market_data.phase6_statistics import (
    bootstrap_mean_ci, descriptive_returns, exact_sign_test_pvalue, validate_benchmark_mapping,
)


def test_lags_same_day_two_business_days_weekend_holiday_and_negative():
    frame = pd.DataFrame({
        "transaction_date": ["2017-03-13", "2017-03-13", "2017-03-10", "2017-07-03", "2017-03-14"],
        "filing_date": ["2017-03-13", "2017-03-15", "2017-03-13", "2017-07-05", "2017-03-13"],
    })
    result = add_lag_fields(frame)
    assert result["calendar_days_to_disclose"].tolist() == [0, 2, 3, 2, -1]
    assert result["business_days_to_disclose"].tolist() == [0, 2, 1, 1, -1]
    assert result.iloc[-1]["lag_quality_code"] == "NEGATIVE_LAG"


def test_exchange_sessions_friday_holiday_exception_and_early_close():
    cal = UsEquityCalendar()
    assert cal.next_session(date(2017, 3, 10)).isoformat() == "2017-03-13"
    assert cal.next_session(date(2017, 7, 3)).isoformat() == "2017-07-05"
    assert not cal.is_session(date(2012, 10, 29))  # Hurricane Sandy
    assert cal.early_close(date(2017, 11, 24))
    assert cal.close_time(date(2017, 11, 24)).hour == 13


@pytest.mark.parametrize("stamp,expected", [
    ("2017-03-13T08:00:00-04:00", "BEFORE_MARKET_OPEN"),
    ("2017-03-13T12:00:00-04:00", "DURING_REGULAR_HOURS"),
    ("2017-03-13T17:31:00-04:00", "AFTER_MARKET_CLOSE"),
    ("2017-03-12T12:00:00-04:00", "NON_TRADING_DAY"),
])
def test_acceptance_timing_classes_and_dst(stamp, expected):
    assert classify_acceptance(stamp, UsEquityCalendar()) == expected


def transaction_fixture():
    return pd.DataFrame([
        {"transaction_id": "t1", "accession_number": "a1", "issuer_cik": "1", "ticker_clean": "AAA",
         "insider_cik": "o1", "filing_date": "2017-03-10", "is_purchase": 1, "is_sale": 0,
         "purchase_value": 100.0, "sale_value": 0.0, "signed_value": 100.0, "is_amendment": 0},
        {"transaction_id": "t2", "accession_number": "a1", "issuer_cik": "1", "ticker_clean": "AAA",
         "insider_cik": "o2", "filing_date": "2017-03-10", "is_purchase": 0, "is_sale": 1,
         "purchase_value": 0.0, "sale_value": 40.0, "signed_value": -40.0, "is_amendment": 0},
        {"transaction_id": "t3", "accession_number": "a2", "issuer_cik": "1", "ticker_clean": "AAA",
         "insider_cik": "o1", "filing_date": "2017-03-10", "is_purchase": 1, "is_sale": 0,
         "purchase_value": 25.0, "sale_value": 0.0, "signed_value": 25.0, "is_amendment": 1},
    ])


def metadata_fixture():
    return pd.DataFrame([
        {"accession_number": "a1", "form_type": "4", "acceptance_datetime_utc": "2017-03-10T20:00:00+00:00",
         "acceptance_datetime_new_york": "2017-03-10T15:00:00-05:00", "verification_status": "VERIFIED_RAW"},
        {"accession_number": "a2", "form_type": "4/A", "acceptance_datetime_utc": "2017-03-13T22:00:00+00:00",
         "acceptance_datetime_new_york": "2017-03-13T18:00:00-04:00", "verification_status": "VERIFIED_RAW"},
    ])


def test_multiple_transactions_filings_amendment_and_issuer_session_aggregation():
    filings = construct_filing_events(transaction_fixture(), metadata_fixture())
    assert len(filings) == 2
    assert filings.loc[filings.accession_number == "a1", "transaction_count"].item() == 2
    assert filings.loc[filings.accession_number == "a2", "amendment"].item() == 1
    assert set(filings["entry_session"]) == {"2017-03-13"}
    aggregated = aggregate_issuer_sessions(filings)
    assert len(aggregated) == 1
    assert aggregated.iloc[0]["filing_count"] == 2
    assert aggregated.iloc[0]["accession_numbers"] == "a1|a2"
    assert aggregated.iloc[0]["net_reported_transaction_value"] == 85.0


def price_fixture(count=200):
    dates = pd.bdate_range("2017-01-02", periods=count).strftime("%Y-%m-%d")
    return pd.DataFrame({"identifier": "AAA", "trading_date": dates,
                         "adjusted_close": np.arange(100.0, 100.0 + count)})


def test_exact_forward_session_offsets_and_right_censoring():
    prices = price_fixture()
    entry = prices.iloc[5]["trading_date"]
    events = pd.DataFrame([{"event_id": "e1", "identifier": "AAA", "entry_session": entry}])
    result = forward_returns(events, prices)
    for horizon in (1, 5, 20, 60, 90, 180):
        assert result.iloc[0][f"forward_return_{horizon}d"] == pytest.approx((105 + horizon) / 105 - 1)
        assert result.iloc[0][f"return_status_{horizon}d"] == "COMPLETE"
    near_end = pd.DataFrame([{"event_id": "e2", "identifier": "AAA", "entry_session": prices.iloc[-2]["trading_date"]}])
    censored = forward_returns(near_end, prices)
    assert censored.iloc[0]["return_status_1d"] == "COMPLETE"
    assert censored.iloc[0]["return_status_5d"] == "RIGHT_CENSORED"


def test_missing_entry_duplicate_invalid_price_and_adjusted_action_series():
    prices = price_fixture(10)
    event = pd.DataFrame([{"event_id": "e", "identifier": "AAA", "entry_session": "2016-12-30"}])
    assert forward_returns(event, prices, (1,)).iloc[0]["return_status_1d"] == "MISSING_ENTRY_PRICE"
    with pytest.raises(ValueError, match="Duplicate"):
        validate_price_series(pd.concat([prices, prices.iloc[[0]]]))
    invalid = prices.copy(); invalid.loc[0, "adjusted_close"] = 0
    with pytest.raises(ValueError, match="positive"):
        validate_price_series(invalid)
    # A validated adjusted series remains continuous through a synthetic split/dividend.
    action_prices = pd.DataFrame({"identifier": ["AAA"] * 3, "trading_date": ["d0", "d1", "d2"],
                                  "adjusted_close": [100.0, 101.0, 102.0]})
    action_event = pd.DataFrame([{"event_id": "e", "identifier": "AAA", "entry_session": "d0"}])
    assert forward_returns(action_event, action_prices, (2,)).iloc[0]["forward_return_2d"] == pytest.approx(.02)


def test_benchmark_alignment_missing_benchmark_and_stable_ids():
    prices = price_fixture(10)
    entry = prices.iloc[0]["trading_date"]
    stock_event = pd.DataFrame([{"event_id": "e", "identifier": "AAA", "entry_session": entry,
                                 "benchmark_identifier": "SPY"}])
    stock = forward_returns(stock_event, prices, (1,))
    benchmark = prices.assign(identifier="SPY", adjusted_close=np.arange(200.0, 210.0))
    result = benchmark_adjusted(stock, benchmark, (1,))
    expected = (101 / 100 - 1) - (201 / 200 - 1)
    assert result.iloc[0]["alpha_vs_benchmark_1d"] == pytest.approx(expected)
    with pytest.raises(ValueError, match="Stock return missing"):
        benchmark_adjusted(stock.drop(columns="forward_return_1d"), benchmark, (1,))
    assert stable_id("filing", "a") == stable_id("filing", "a")
    assert stable_id("filing", "a") != stable_id("filing", "b")


def test_same_issuer_overlapping_windows_are_flagged_deterministically():
    events = pd.DataFrame([
        {"issuer_cik": "1", "entry_session": "2017-03-13"},
        {"issuer_cik": "1", "entry_session": "2017-03-14"},
        {"issuer_cik": "2", "entry_session": "2017-03-14"},
    ])
    first = flag_overlaps(events, 5)
    second = flag_overlaps(events, 5)
    assert first.tolist() == [True, True, False]
    assert first.equals(second)


def test_statistical_interfaces_are_deterministic_and_do_not_overstate_sign_test():
    values = pd.Series([.10, .20, -.10, np.nan])
    assert descriptive_returns(values)["count"] == 3
    assert bootstrap_mean_ci(values, iterations=100, seed=42) == bootstrap_mean_ci(values, iterations=100, seed=42)
    assert 0 <= exact_sign_test_pvalue(values) <= 1


def test_point_in_time_benchmark_mapping_rejects_nonexistent_or_wrong_interval():
    validate_benchmark_mapping("2017-03-13", "XLK", "1998-12-22", None, "dated classification evidence")
    with pytest.raises(ValueError, match="did not exist"):
        validate_benchmark_mapping("1997-01-01", "XLK", "1998-12-22", None, "rationale")
    with pytest.raises(ValueError, match="required"):
        validate_benchmark_mapping("2017-01-01", None, None, None, None)
    with pytest.raises(ValueError, match="no longer valid"):
        validate_benchmark_mapping("2018-01-01", "OLD", "2000-01-01", "2017-12-31", "historical mapping")
