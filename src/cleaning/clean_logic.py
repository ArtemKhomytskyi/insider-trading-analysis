"""Core transformation logic for clean_insider_transactions (testable, no I/O)."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Any

from src.utils.role_taxonomy import classify_role
from src.utils.sec_taxonomy import get_transaction_code_metadata
from src.utils.security_taxonomy import classify_security_title

INVALID_TICKER_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "",
        "NONE",
        "N/A",
        "NA",
        "NULL",
        "NOT APPLICABLE",
        "NO SYMBOL",
        "UNSPECIFIED",
    }
)

DEFAULT_CLEANING_CONFIG: dict[str, Any] = {
    "minimum_transaction_value": 10_000,
    "tiny_transaction_value": 10_000,
    "extreme_transaction_value": 10_000_000_000,
    "include_amendments_in_mvp": False,
    "allowed_document_types_for_mvp": ["4"],
    "allowed_transaction_codes_for_mvp": ["P", "S"],
    "exclude_stale_filings_from_mvp": False,
    "stale_filing_lag_days": 365,
}


def load_cleaning_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    if not config:
        return dict(DEFAULT_CLEANING_CONFIG)
    merged = dict(DEFAULT_CLEANING_CONFIG)
    merged.update(config)
    return merged


def normalize_insider_name(raw_name: str | None) -> str:
    if raw_name is None or str(raw_name).strip() == "":
        return ""
    text = str(raw_name).upper()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_ticker_raw(raw: str | None) -> str:
    return str(raw or "").strip()


def clean_ticker(raw: str | None) -> str:
    text = normalize_ticker_raw(raw).upper()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_invalid_ticker_placeholder(ticker_clean: str) -> bool:
    if not ticker_clean or not ticker_clean.strip():
        return True
    normalized = re.sub(r"[\s.\-/]", "", ticker_clean.upper())
    if normalized in {"NA", "NONE", "NULL", "UNSPECIFIED", "NOSYMBOL", "NOTAPPLICABLE"}:
        return True
    return ticker_clean.upper().strip() in INVALID_TICKER_PLACEHOLDERS


def is_multisymbol_ticker(ticker_clean: str) -> bool:
    if not ticker_clean:
        return False
    if any(sep in ticker_clean for sep in (",", ";", "/")):
        return True
    return False


def classify_ticker(raw: str | None) -> dict[str, Any]:
    ticker_raw = normalize_ticker_raw(raw)
    ticker_clean = clean_ticker(raw)
    is_placeholder = is_invalid_ticker_placeholder(ticker_clean)
    is_multisymbol = is_multisymbol_ticker(ticker_clean)
    is_valid = bool(ticker_clean) and not is_placeholder and not is_multisymbol
    return {
        "ticker_raw": ticker_raw,
        "ticker_clean": ticker_clean,
        "is_valid_ticker": is_valid,
        "is_invalid_ticker_placeholder": is_placeholder,
        "is_multisymbol_ticker": is_multisymbol,
    }


def assign_filing_lag_bucket(filing_lag_days: int | None) -> str:
    if filing_lag_days is None or filing_lag_days < 0:
        return "NEGATIVE_OR_INVALID"
    if filing_lag_days == 0:
        return "SAME_DAY"
    if filing_lag_days <= 2:
        return "ONE_TWO_DAYS"
    if filing_lag_days <= 7:
        return "THREE_TO_SEVEN_DAYS"
    if filing_lag_days <= 30:
        return "EIGHT_TO_THIRTY_DAYS"
    if filing_lag_days <= 365:
        return "THIRTY_TO_365_DAYS"
    return "OVER_365_DAYS"


def compute_filing_quality_flags(
    filing_lag_days: int | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    stale_threshold = int(config.get("stale_filing_lag_days", 365))
    is_stale = filing_lag_days is not None and filing_lag_days > stale_threshold
    return {
        "is_stale_filing": is_stale,
        "filing_lag_bucket": assign_filing_lag_bucket(filing_lag_days),
    }


def compute_transaction_year_flags(
    transaction_date: date | None,
    filing_date: date | None,
    source_year: Any,
) -> dict[str, Any]:
    transaction_year = transaction_date.year if transaction_date else None
    filing_year = filing_date.year if filing_date else None
    src_year: int | None = None
    if source_year is not None and str(source_year).strip() != "":
        try:
            src_year = int(source_year)
        except (TypeError, ValueError):
            src_year = None

    gap: int | None = None
    is_pre_source = False
    if transaction_year is not None and src_year is not None:
        gap = src_year - transaction_year
        is_pre_source = transaction_year < src_year

    return {
        "transaction_year": transaction_year,
        "filing_year": filing_year,
        "transaction_year_source_gap": gap,
        "is_pre_source_transaction_date": is_pre_source,
    }


def date_to_iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def parse_document_type_flags(document_type: str | None) -> dict[str, bool]:
    text = str(document_type or "").strip().upper()
    base = text.split("/")[0] if text else ""
    return {
        "is_form_3": base == "3",
        "is_form_4": base == "4",
        "is_form_5": base == "5",
        "is_amendment": "/A" in text,
        "document_type_base": base,
    }


def compute_transaction_id(
    accession_number: str,
    nonderiv_trans_sk: str,
    insider_cik: str | None,
    row_index: int | None = None,
) -> str:
    insider_key = (insider_cik or "").strip() or f"NO_OWNER_{row_index or 0}"
    payload = f"{accession_number}|{nonderiv_trans_sk}|{insider_key}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_filing_lag_days(
    transaction_date: date | None,
    filing_date: date | None,
) -> int | None:
    if transaction_date is None or filing_date is None:
        return None
    return (filing_date - transaction_date).days


def compute_quality_flags(
    shares: float | None,
    price_per_share: float | None,
    transaction_value: float | None,
    transaction_date: date | None,
    filing_date: date | None,
    filing_lag_days: int | None,
    config: dict[str, Any],
) -> dict[str, bool]:
    extreme_threshold = float(config["extreme_transaction_value"])
    tiny_threshold = float(config["tiny_transaction_value"])

    is_missing_price = price_per_share is None
    is_zero_price = price_per_share is not None and price_per_share == 0
    is_negative_or_zero_shares = shares is None or shares <= 0
    is_missing_transaction_date = transaction_date is None
    is_missing_filing_date = filing_date is None
    is_filing_before_transaction = (
        filing_lag_days is not None and filing_lag_days < 0
    )
    is_extreme = (
        transaction_value is not None and transaction_value > extreme_threshold
    )
    is_tiny = (
        transaction_value is not None
        and transaction_value < tiny_threshold
    )

    is_valid = (
        not is_missing_transaction_date
        and not is_missing_filing_date
        and shares is not None
        and shares > 0
        and price_per_share is not None
        and price_per_share > 0
        and filing_lag_days is not None
        and filing_lag_days >= 0
        and transaction_value is not None
        and not is_extreme
    )

    return {
        "is_missing_price": is_missing_price,
        "is_zero_price": is_zero_price,
        "is_negative_or_zero_shares": is_negative_or_zero_shares,
        "is_missing_transaction_date": is_missing_transaction_date,
        "is_missing_filing_date": is_missing_filing_date,
        "is_filing_before_transaction": is_filing_before_transaction,
        "is_extreme_transaction_value": is_extreme,
        "is_tiny_transaction": is_tiny,
        "is_valid_clean_event": is_valid,
    }


def compute_include_mvp_alpha(
    doc_flags: dict[str, bool],
    trans_meta: dict[str, Any] | None,
    sec_meta: dict[str, Any],
    quality: dict[str, bool],
    ticker_meta: dict[str, Any],
    filing_meta: dict[str, Any],
    transaction_value: float | None,
    config: dict[str, Any],
) -> bool:
    if not quality["is_valid_clean_event"]:
        return False
    if transaction_value is None:
        return False
    if transaction_value < float(config["minimum_transaction_value"]):
        return False

    allowed_docs = {str(d).strip() for d in config["allowed_document_types_for_mvp"]}
    if doc_flags["document_type_base"] not in allowed_docs:
        return False
    if doc_flags["is_amendment"] and not config.get("include_amendments_in_mvp", False):
        return False

    if not trans_meta or not trans_meta.get("include_clean_mvp"):
        return False
    if not sec_meta.get("include_clean_mvp"):
        return False

    allowed_codes = {str(c).strip().upper() for c in config["allowed_transaction_codes_for_mvp"]}
    trans_code = str(trans_meta.get("transaction_code", "")).upper()
    if trans_code not in allowed_codes:
        return False

    if not ticker_meta["is_valid_ticker"]:
        return False
    if ticker_meta["is_multisymbol_ticker"]:
        return False

    if config.get("exclude_stale_filings_from_mvp", False) and filing_meta["is_stale_filing"]:
        return False

    return True


def transform_row(
    row: dict[str, Any],
    config: dict[str, Any],
    row_index: int | None = None,
) -> dict[str, Any]:
    """Transform one joined raw row into a clean_insider_transactions record."""
    from src.utils.sec_parsing import parse_sec_date, parse_sec_number

    accession = str(row.get("ACCESSION_NUMBER", "") or "")
    nonderiv_sk = str(row.get("NONDERIV_TRANS_SK", "") or "")
    insider_cik = str(row.get("RPTOWNERCIK", "") or "").strip() or None

    trans_date = parse_sec_date(row.get("TRANS_DATE"))
    filing_date = parse_sec_date(row.get("FILING_DATE"))
    period_date = parse_sec_date(row.get("PERIOD_OF_REPORT"))
    filing_lag = compute_filing_lag_days(trans_date, filing_date)

    shares = parse_sec_number(row.get("TRANS_SHARES"))
    price = parse_sec_number(row.get("TRANS_PRICEPERSHARE"))
    shares_following = parse_sec_number(row.get("SHRS_OWND_FOLWNG_TRANS"))
    transaction_value = (
        shares * price if shares is not None and price is not None else None
    )

    trans_code = str(row.get("TRANS_CODE", "") or "").strip().upper()
    trans_meta = get_transaction_code_metadata(trans_code)
    if trans_meta is None:
        trans_meta = {
            "transaction_code": trans_code,
            "meaning": "",
            "category": "UNKNOWN",
            "signal_direction": "UNKNOWN",
            "signal_quality": "LOW",
            "include_clean_mvp": False,
        }
    else:
        trans_meta = {
            "transaction_code": trans_code,
            "meaning": trans_meta.get("meaning", ""),
            "category": trans_meta.get("category", ""),
            "signal_direction": trans_meta.get("signal_direction", ""),
            "signal_quality": trans_meta.get("signal_quality", ""),
            "include_clean_mvp": trans_meta.get("include_clean_mvp", False),
        }

    sec_meta = classify_security_title(row.get("SECURITY_TITLE"))
    role_meta = classify_role(
        row.get("RPTOWNER_TITLE"),
        row.get("RPTOWNER_RELATIONSHIP"),
    )

    doc_flags = parse_document_type_flags(row.get("DOCUMENT_TYPE"))
    quality = compute_quality_flags(
        shares, price, transaction_value, trans_date, filing_date, filing_lag, config
    )
    ticker_meta = classify_ticker(row.get("ISSUERTRADINGSYMBOL"))
    filing_meta = compute_filing_quality_flags(filing_lag, config)
    year_meta = compute_transaction_year_flags(
        trans_date, filing_date, row.get("source_year")
    )

    is_purchase = trans_code == "P"
    is_sale = trans_code == "S"
    is_open_market = trans_meta["category"] == "OPEN_MARKET" or is_purchase or is_sale

    include_mvp = compute_include_mvp_alpha(
        doc_flags,
        trans_meta,
        sec_meta,
        quality,
        ticker_meta,
        filing_meta,
        transaction_value,
        config,
    )

    created_at = datetime.now(timezone.utc).isoformat()

    record = {
        "transaction_id": compute_transaction_id(accession, nonderiv_sk, insider_cik, row_index),
        "accession_number": accession,
        "nonderiv_trans_sk": nonderiv_sk,
        "document_type": str(row.get("DOCUMENT_TYPE", "") or "").strip(),
        "is_form_3": int(doc_flags["is_form_3"]),
        "is_form_4": int(doc_flags["is_form_4"]),
        "is_form_5": int(doc_flags["is_form_5"]),
        "is_amendment": int(doc_flags["is_amendment"]),
        "issuer_cik": str(row.get("ISSUERCIK", "") or "").strip(),
        "issuer_name": str(row.get("ISSUERNAME", "") or "").strip(),
        "ticker": ticker_meta["ticker_clean"],
        "ticker_raw": ticker_meta["ticker_raw"],
        "ticker_clean": ticker_meta["ticker_clean"],
        "is_valid_ticker": int(ticker_meta["is_valid_ticker"]),
        "is_invalid_ticker_placeholder": int(ticker_meta["is_invalid_ticker_placeholder"]),
        "is_multisymbol_ticker": int(ticker_meta["is_multisymbol_ticker"]),
        "insider_cik": insider_cik or "",
        "insider_name": str(row.get("RPTOWNERNAME", "") or "").strip(),
        "insider_name_clean": normalize_insider_name(row.get("RPTOWNERNAME")),
        "raw_relationship": str(row.get("RPTOWNER_RELATIONSHIP", "") or "").strip(),
        "raw_role_title": str(row.get("RPTOWNER_TITLE", "") or "").strip(),
        "normalized_role": role_meta["normalized_role"],
        "role_group": role_meta["role_group"],
        "role_weight": float(role_meta["role_weight"]),
        "is_executive": int(role_meta["is_executive"]),
        "is_board_member": int(role_meta["is_board_member"]),
        "is_large_owner": int(role_meta["is_large_owner"]),
        "role_requires_review": int(role_meta["requires_review"]),
        "role_confidence": role_meta["confidence"],
        "transaction_date": date_to_iso(trans_date),
        "filing_date": date_to_iso(filing_date),
        "period_of_report": date_to_iso(period_date),
        "filing_lag_days": filing_lag,
        "is_stale_filing": int(filing_meta["is_stale_filing"]),
        "filing_lag_bucket": filing_meta["filing_lag_bucket"],
        "transaction_year": year_meta["transaction_year"],
        "filing_year": year_meta["filing_year"],
        "is_pre_source_transaction_date": int(year_meta["is_pre_source_transaction_date"]),
        "transaction_year_source_gap": year_meta["transaction_year_source_gap"],
        "transaction_code": trans_code,
        "transaction_meaning": trans_meta["meaning"],
        "transaction_category": trans_meta["category"],
        "signal_direction": trans_meta["signal_direction"],
        "signal_quality": trans_meta["signal_quality"],
        "transaction_include_clean_mvp": int(bool(trans_meta["include_clean_mvp"])),
        "security_title_raw": str(row.get("SECURITY_TITLE", "") or "").strip(),
        "security_title_normalized": sec_meta["normalized_title"],
        "normalized_security_type": sec_meta["normalized_security_type"],
        "security_include_clean_mvp": int(bool(sec_meta["include_clean_mvp"])),
        "security_requires_review": int(bool(sec_meta["requires_review"])),
        "security_confidence": sec_meta["confidence"],
        "acquired_disposed_code": str(row.get("TRANS_ACQUIRED_DISP_CD", "") or "").strip(),
        "direct_indirect_ownership": str(row.get("DIRECT_INDIRECT_OWNERSHIP", "") or "").strip(),
        "nature_of_ownership": str(row.get("NATURE_OF_OWNERSHIP", "") or "").strip(),
        "shares": shares,
        "price_per_share": price,
        "shares_owned_following": shares_following,
        "transaction_value": transaction_value,
        "is_purchase": int(is_purchase),
        "is_sale": int(is_sale),
        "is_open_market": int(is_open_market),
        "is_missing_price": int(quality["is_missing_price"]),
        "is_zero_price": int(quality["is_zero_price"]),
        "is_negative_or_zero_shares": int(quality["is_negative_or_zero_shares"]),
        "is_missing_transaction_date": int(quality["is_missing_transaction_date"]),
        "is_missing_filing_date": int(quality["is_missing_filing_date"]),
        "is_filing_before_transaction": int(quality["is_filing_before_transaction"]),
        "is_extreme_transaction_value": int(quality["is_extreme_transaction_value"]),
        "is_tiny_transaction": int(quality["is_tiny_transaction"]),
        "is_valid_clean_event": int(quality["is_valid_clean_event"]),
        "include_mvp_alpha": int(include_mvp),
        "source_year": row.get("source_year"),
        "source_quarter": row.get("source_quarter"),
        "source_file": str(row.get("source_file", "") or "").strip(),
        "source_folder": str(row.get("source_folder", "") or "").strip(),
        "created_at": created_at,
    }
    return record


CLEAN_COLUMNS: tuple[str, ...] = (
    "transaction_id",
    "accession_number",
    "nonderiv_trans_sk",
    "document_type",
    "is_form_3",
    "is_form_4",
    "is_form_5",
    "is_amendment",
    "issuer_cik",
    "issuer_name",
    "ticker",
    "ticker_raw",
    "ticker_clean",
    "is_valid_ticker",
    "is_invalid_ticker_placeholder",
    "is_multisymbol_ticker",
    "insider_cik",
    "insider_name",
    "insider_name_clean",
    "raw_relationship",
    "raw_role_title",
    "normalized_role",
    "role_group",
    "role_weight",
    "is_executive",
    "is_board_member",
    "is_large_owner",
    "role_requires_review",
    "role_confidence",
    "transaction_date",
    "filing_date",
    "period_of_report",
    "filing_lag_days",
    "is_stale_filing",
    "filing_lag_bucket",
    "transaction_year",
    "filing_year",
    "is_pre_source_transaction_date",
    "transaction_year_source_gap",
    "transaction_code",
    "transaction_meaning",
    "transaction_category",
    "signal_direction",
    "signal_quality",
    "transaction_include_clean_mvp",
    "security_title_raw",
    "security_title_normalized",
    "normalized_security_type",
    "security_include_clean_mvp",
    "security_requires_review",
    "security_confidence",
    "acquired_disposed_code",
    "direct_indirect_ownership",
    "nature_of_ownership",
    "shares",
    "price_per_share",
    "shares_owned_following",
    "transaction_value",
    "is_purchase",
    "is_sale",
    "is_open_market",
    "is_missing_price",
    "is_zero_price",
    "is_negative_or_zero_shares",
    "is_missing_transaction_date",
    "is_missing_filing_date",
    "is_filing_before_transaction",
    "is_extreme_transaction_value",
    "is_tiny_transaction",
    "is_valid_clean_event",
    "include_mvp_alpha",
    "source_year",
    "source_quarter",
    "source_file",
    "source_folder",
    "created_at",
)
