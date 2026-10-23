"""Offline tests for the final EDGAR integrity/audit export."""

from datetime import datetime
import hashlib

import pytest

from src.market_data import edgar_remaining
from src.market_data.edgar_final_audit import (
    authorized_controlled_transition, checkpoint_reconciles, convert_acceptance,
)
from src.market_data.edgar_timestamp_pilot import archive_url


def test_acceptance_conversion_uses_new_york_dst_rules():
    winter_utc, winter_ny = convert_acceptance("20060103160534")
    summer_utc, summer_ny = convert_acceptance("20170615160534")
    assert datetime.fromisoformat(winter_utc).isoformat() == "2006-01-03T21:05:34+00:00"
    assert winter_ny == "2006-01-03T16:05:34-05:00"
    assert datetime.fromisoformat(summer_utc).isoformat() == "2017-06-15T20:05:34+00:00"
    assert summer_ny == "2017-06-15T16:05:34-04:00"


@pytest.mark.parametrize("raw", ["", "20260101", "not-a-time"])
def test_acceptance_conversion_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        convert_acceptance(raw)


def test_acceptance_conversion_does_not_guess_ambiguous_dst_time():
    with pytest.raises(ValueError, match="ambiguous"):
        convert_acceptance("20171105013000")


def controlled_fixture():
    row = {
        "accession_number": edgar_remaining.EXHAUSTED_ACCESSION,
        "archive_cik_candidate": "1207626",
        "issuer_cik": "0001108524",
        "reporting_owner_ciks": "0001207626",
        "document_type": "4",
        "filing_date": "2006-01-03",
    }
    body = (f"<SEC-HEADER>\nACCESSION NUMBER: {row['accession_number']}\n"
            "<ACCEPTANCE-DATETIME>20060103194311\nCONFORMED SUBMISSION TYPE: 4\n"
            "FILED AS OF DATE: 20060103\nCENTRAL INDEX KEY: 1108524\n"
            "CENTRAL INDEX KEY: 1207626\n</SEC-HEADER>").encode()
    history = [
        {"database": "edgar_timestamp_pilot_20.db", "status": "FAILED", "attempts": 3},
        {"database": edgar_remaining.CONTROLLED_DB.name, "status": "VERIFIED_RAW", "attempts": 1,
         "http_status": 200, "body": body, "source_url": archive_url(row),
         "raw_sha256": hashlib.sha256(body).hexdigest(), "raw_acceptance": "20060103194311"},
    ]
    return row, history


def test_authorized_failed_to_verified_controlled_transition():
    row, history = controlled_fixture()
    assert authorized_controlled_transition(row["accession_number"], row, history)[0] is True


@pytest.mark.parametrize("mutation", ["accession", "database", "attempts", "http", "sha", "header"])
def test_invalid_or_mismatched_controlled_evidence_is_not_superseding(mutation):
    row, history = controlled_fixture()
    accession = row["accession_number"]
    if mutation == "accession":
        accession = "0001305763-06-000003"
    elif mutation == "database":
        history[1]["database"] = "unexpected.db"
    elif mutation == "attempts":
        history[1]["attempts"] = 2
    elif mutation == "http":
        history[1]["http_status"] = 404
    elif mutation == "sha":
        history[1]["raw_sha256"] = "0" * 64
    else:
        history[1]["body"] = history[1]["body"].replace(b"1108524", b"9999999")
        history[1]["raw_sha256"] = hashlib.sha256(history[1]["body"]).hexdigest()
    assert authorized_controlled_transition(accession, row, history)[0] is False


def test_checkpoint_reconciles_historical_totals_plus_controlled_delta():
    checkpoint = {"plan_sha256": "plan", "verified_total": 6066, "controlled_retry": 1,
                  "remaining_normal": 0, "state": "COMPLETED", "next_batch": None,
                  "http_attempts_total": 6087}
    assert checkpoint_reconciles(checkpoint, "plan", 6067, 6067, 6088, 1, 1)
    assert not checkpoint_reconciles(checkpoint, "different", 6067, 6067, 6088, 1, 1)
    assert not checkpoint_reconciles(checkpoint, "plan", 6067, 6067, 6089, 1, 1)
