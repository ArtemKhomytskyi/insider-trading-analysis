import hashlib
from datetime import date, timedelta

import pandas as pd
import pytest

from src.market_data.phase6b_market_data import (
    BenchmarkMapping, SecurityIdentity, evaluate_gates, resolve_benchmark,
    resolve_identity, store_snapshot, validated_returns, validate_actions,
)
from src.market_data.phase6_engine import UsEquityCalendar


def fixture():
    identity = SecurityIdentity("sec1", "1", "AAA", "AAA", "2010-01-01", None,
                                "USD", "XNYS", "COMMON", "dated filing")
    sessions = UsEquityCalendar().sessions(date(2017, 1, 3), date(2017, 12, 31))[:181]
    prices = pd.DataFrame({"identifier": ["sec1"] * 181,
                           "trading_date": [day.isoformat() for day in sessions],
                           "adjusted_close": [100.0 + i for i in range(181)], "currency": ["USD"] * 181})
    manifest = dict(provider="fixture", retrieved_at_utc="2017-12-01T00:00:00Z", source="synthetic",
                    snapshot_sha256="a" * 64, schema_version=1, coverage_start="2017-01-03",
                    coverage_end="2017-09-12", currency="USD", exchange="XNYS",
                    adjustment_policy="VALIDATED_TOTAL_RETURN", storage_rights=True, research_rights=True,
                    actions_validated=True, benchmark_validated=True, snapshot_hash_verified=True,
                    owner_authorized=True, coverage_validated=True)
    return identity, prices, manifest


def test_dated_identity_and_etf_inception():
    identity, _, _ = fixture()
    assert resolve_identity([identity], "1", "2017-01-03") == identity
    with pytest.raises(ValueError, match="missing or ambiguous"):
        resolve_identity([identity], "1", "2009-01-01")
    mapping = BenchmarkMapping("1", "Technology", "ETF", "2010-01-01", None,
                               "2015-01-01", "dated source", "APPROVED")
    with pytest.raises(ValueError, match="predates inception"):
        resolve_benchmark([mapping], "1", "2012-01-01")
    assert resolve_benchmark([mapping], "1", "2017-01-03") == "ETF"


def test_actions_and_fail_closed_gates():
    identity, prices, manifest = fixture()
    actions = pd.DataFrame(columns=["security_id", "event_date", "action_type", "ratio", "cash", "evidence"])
    assert validate_actions(actions, prices) == []
    assert evaluate_gates(manifest, prices, [identity], actions)["G2_RIGHTS"]["status"] == "PASS"
    manifest["research_rights"] = False
    assert evaluate_gates(manifest, prices, [identity], actions)["G2_RIGHTS"]["status"] == "BLOCKED"
    bad = pd.DataFrame([{"security_id": "sec1", "event_date": "2017-01-04",
                         "action_type": "SPLIT", "ratio": 0, "cash": 0, "evidence": ""}])
    assert {x["code"] for x in validate_actions(bad, prices)} == {"INVALID_SPLIT_RATIO", "SPLIT_EVIDENCE_MISSING"}


def test_content_addressed_storage_and_return_adapter(tmp_path):
    identity, prices, manifest = fixture()
    payload = b"date,close\n2017-01-03,100\n"
    manifest["snapshot_sha256"] = hashlib.sha256(payload).hexdigest()
    first = store_snapshot(tmp_path, payload, manifest)
    assert store_snapshot(tmp_path, payload, manifest) == first
    assert (first / "raw.csv").read_bytes() == payload
    with pytest.raises(ValueError, match="hash mismatch"):
        store_snapshot(tmp_path, b"different", manifest)
    actions = pd.DataFrame(columns=["security_id", "event_date", "action_type", "ratio", "cash", "evidence"])
    events = pd.DataFrame([{"event_id": "e", "issuer_cik": "1", "entry_session": "2017-01-03"}])
    result = validated_returns(events, prices, manifest, [identity], actions)
    assert result.iloc[0]["forward_return_1d"] == pytest.approx(.01)
    manifest["adjustment_policy"] = "UNKNOWN"
    with pytest.raises(ValueError, match="G5_ACTIONS"):
        validated_returns(events, prices, manifest, [identity], actions)


def test_adapter_rejects_missing_session_and_benchmark_misalignment():
    identity, prices, manifest = fixture()
    actions = pd.DataFrame(columns=["security_id", "event_date", "action_type", "ratio", "cash", "evidence"])
    events = pd.DataFrame([{"event_id": "e", "issuer_cik": "1", "entry_session": "2017-01-03"}])
    missing_bar = prices.loc[prices["trading_date"] != "2017-01-04"].copy()
    with pytest.raises(ValueError, match="Missing exchange session"):
        validated_returns(events, missing_bar, manifest, [identity], actions)
    benchmark = prices.assign(identifier="ETF")
    with pytest.raises(ValueError, match="Dated benchmark mappings required"):
        validated_returns(events, prices, manifest, [identity], actions, benchmark)
    mapping = BenchmarkMapping("1", "Technology", "ETF", "2010-01-01", None,
                               "2010-01-01", "dated source", "APPROVED")
    with pytest.raises(ValueError, match="coverage mismatch"):
        validated_returns(events, prices, manifest, [identity], actions,
                          benchmark.loc[benchmark["trading_date"] != "2017-01-04"], [mapping])
    result = validated_returns(events, prices, manifest, [identity], actions, benchmark, [mapping])
    assert result.iloc[0]["benchmark_alignment_1d"] == "ALIGNED"
