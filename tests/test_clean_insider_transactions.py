"""Tests for clean insider transaction layer."""

from __future__ import annotations

from datetime import date

import pytest

from src.cleaning.clean_logic import (
    assign_filing_lag_bucket,
    classify_ticker,
    compute_filing_lag_days,
    compute_transaction_id,
    compute_transaction_year_flags,
    load_cleaning_config,
    normalize_insider_name,
    transform_row,
)


def _base_row(**overrides) -> dict:
    row = {
        "ACCESSION_NUMBER": "0001234567-08-000001",
        "NONDERIV_TRANS_SK": "1",
        "TRANS_DATE": "15-JAN-2020",
        "FILING_DATE": "20-JAN-2020",
        "PERIOD_OF_REPORT": "31-DEC-2019",
        "DOCUMENT_TYPE": "4",
        "TRANS_CODE": "P",
        "TRANS_SHARES": "1000",
        "TRANS_PRICEPERSHARE": "25.50",
        "SHRS_OWND_FOLWNG_TRANS": "5000",
        "SECURITY_TITLE": "Common Stock",
        "TRANS_ACQUIRED_DISP_CD": "A",
        "DIRECT_INDIRECT_OWNERSHIP": "D",
        "NATURE_OF_OWNERSHIP": "",
        "ISSUERCIK": "0000320193",
        "ISSUERNAME": "Apple Inc.",
        "ISSUERTRADINGSYMBOL": "AAPL",
        "RPTOWNERCIK": "0001214156",
        "RPTOWNERNAME": "Tim Cook",
        "RPTOWNER_RELATIONSHIP": "Director,Officer",
        "RPTOWNER_TITLE": "Chief Executive Officer",
        "source_year": 2020,
        "source_quarter": 1,
        "source_file": "test.tsv",
        "source_folder": "2020q1",
    }
    row.update(overrides)
    return row


@pytest.fixture
def config():
    return load_cleaning_config()


def test_transaction_id_is_stable() -> None:
    tid1 = compute_transaction_id("acc-1", "sk-1", "123")
    tid2 = compute_transaction_id("acc-1", "sk-1", "123")
    tid3 = compute_transaction_id("acc-1", "sk-1", "456")
    assert tid1 == tid2
    assert tid1 != tid3
    assert len(tid1) == 64


def test_date_parsing(config) -> None:
    record = transform_row(_base_row(), config)
    assert record["transaction_date"] == "2020-01-15"
    assert record["filing_date"] == "2020-01-20"
    assert record["period_of_report"] == "2019-12-31"
    assert record["filing_lag_days"] == 5


def test_numeric_parsing(config) -> None:
    record = transform_row(_base_row(), config)
    assert record["shares"] == 1000.0
    assert record["price_per_share"] == 25.50
    assert record["shares_owned_following"] == 5000.0


def test_transaction_value_computed(config) -> None:
    record = transform_row(_base_row(), config)
    assert record["transaction_value"] == pytest.approx(25500.0)


def test_mvp_alpha_valid_purchase(config) -> None:
    record = transform_row(_base_row(), config)
    assert record["is_valid_clean_event"] == 1
    assert record["include_mvp_alpha"] == 1
    assert record["is_form_4"] == 1
    assert record["transaction_code"] == "P"
    assert record["normalized_security_type"] == "COMMON_EQUITY"


def test_form_3_preserved_not_mvp(config) -> None:
    record = transform_row(_base_row(DOCUMENT_TYPE="3"), config)
    assert record["is_form_3"] == 1
    assert record["include_mvp_alpha"] == 0
    assert record["accession_number"]


def test_form_5_preserved_not_mvp(config) -> None:
    record = transform_row(_base_row(DOCUMENT_TYPE="5"), config)
    assert record["is_form_5"] == 1
    assert record["include_mvp_alpha"] == 0


@pytest.mark.parametrize("code", ["A", "M", "F"])
def test_non_open_market_codes_not_mvp(config, code: str) -> None:
    record = transform_row(_base_row(TRANS_CODE=code), config)
    assert record["include_mvp_alpha"] == 0
    assert record["transaction_code"] == code


def test_rsu_security_not_mvp(config) -> None:
    record = transform_row(
        _base_row(SECURITY_TITLE="Restricted Stock Units"),
        config,
    )
    assert record["include_mvp_alpha"] == 0
    assert record["normalized_security_type"] == "STOCK_COMPENSATION"


def test_option_security_not_mvp(config) -> None:
    record = transform_row(
        _base_row(SECURITY_TITLE="Stock Option (Right to Buy)"),
        config,
    )
    assert record["include_mvp_alpha"] == 0


def test_zero_price_invalid_event(config) -> None:
    record = transform_row(_base_row(TRANS_PRICEPERSHARE="0"), config)
    assert record["is_zero_price"] == 1
    assert record["is_valid_clean_event"] == 0
    assert record["include_mvp_alpha"] == 0


def test_negative_filing_lag_invalid(config) -> None:
    record = transform_row(
        _base_row(TRANS_DATE="20-JAN-2020", FILING_DATE="15-JAN-2020"),
        config,
    )
    assert record["is_filing_before_transaction"] == 1
    assert compute_filing_lag_days(date(2020, 1, 20), date(2020, 1, 15)) == -5
    assert record["is_valid_clean_event"] == 0
    assert record["include_mvp_alpha"] == 0


def test_tiny_transaction_below_threshold(config) -> None:
    record = transform_row(
        _base_row(TRANS_SHARES="10", TRANS_PRICEPERSHARE="50"),
        config,
    )
    assert record["transaction_value"] == pytest.approx(500.0)
    assert record["is_tiny_transaction"] == 1
    assert record["include_mvp_alpha"] == 0


def test_amendment_not_mvp(config) -> None:
    record = transform_row(_base_row(DOCUMENT_TYPE="4/A"), config)
    assert record["is_amendment"] == 1
    assert record["include_mvp_alpha"] == 0


def test_insider_name_clean(config) -> None:
    record = transform_row(_base_row(RPTOWNERNAME="Dr. Jane Q. Public"), config)
    assert record["insider_name_clean"] == "DR JANE Q PUBLIC"
    assert normalize_insider_name("Dr. Jane Q. Public") == "DR JANE Q PUBLIC"


@pytest.mark.parametrize("raw", ["NONE", "none", "n/a", "N/A", ""])
def test_invalid_ticker_placeholders(config, raw: str) -> None:
    meta = classify_ticker(raw)
    assert meta["is_invalid_ticker_placeholder"] is True
    assert meta["is_valid_ticker"] is False
    record = transform_row(_base_row(ISSUERTRADINGSYMBOL=raw), config)
    assert record["include_mvp_alpha"] == 0


def test_valid_ticker_aapl(config) -> None:
    meta = classify_ticker("AAPL")
    assert meta["is_valid_ticker"] is True
    assert meta["ticker_clean"] == "AAPL"
    record = transform_row(_base_row(ISSUERTRADINGSYMBOL="  aapl  "), config)
    assert record["ticker_clean"] == "AAPL"
    assert record["include_mvp_alpha"] == 1


def test_multisymbol_ticker_excluded_from_mvp(config) -> None:
    meta = classify_ticker("ISCA, ISCB")
    assert meta["is_multisymbol_ticker"] is True
    assert meta["is_valid_ticker"] is False
    record = transform_row(_base_row(ISSUERTRADINGSYMBOL="ISCA, ISCB"), config)
    assert record["include_mvp_alpha"] == 0


def test_stale_filing_flag(config) -> None:
    record = transform_row(
        _base_row(TRANS_DATE="01-JAN-2020", FILING_DATE="01-JAN-2022"),
        config,
    )
    assert record["filing_lag_days"] > 365
    assert record["is_stale_filing"] == 1
    assert record["filing_lag_bucket"] == "OVER_365_DAYS"


@pytest.mark.parametrize(
    ("lag", "bucket"),
    [
        (0, "SAME_DAY"),
        (1, "ONE_TWO_DAYS"),
        (5, "THREE_TO_SEVEN_DAYS"),
        (15, "EIGHT_TO_THIRTY_DAYS"),
        (100, "THIRTY_TO_365_DAYS"),
        (400, "OVER_365_DAYS"),
        (-1, "NEGATIVE_OR_INVALID"),
        (None, "NEGATIVE_OR_INVALID"),
    ],
)
def test_filing_lag_bucket(lag: int | None, bucket: str) -> None:
    assert assign_filing_lag_bucket(lag) == bucket


def test_pre_source_transaction_date_flag(config) -> None:
    record = transform_row(
        _base_row(TRANS_DATE="15-JAN-2010", source_year=2020),
        config,
    )
    assert record["transaction_year"] == 2010
    assert record["transaction_year_source_gap"] == 10
    assert record["is_pre_source_transaction_date"] == 1

    flags = compute_transaction_year_flags(
        __import__("datetime").date(2010, 1, 15),
        __import__("datetime").date(2020, 1, 20),
        2020,
    )
    assert flags["is_pre_source_transaction_date"] is True
