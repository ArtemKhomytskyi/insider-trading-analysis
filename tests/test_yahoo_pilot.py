"""Offline Yahoo pilot tests using local SQLite and synthetic daily bars."""

import csv
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile

import pandas as pd
import pytest

from src.market_data.market_data_models import DailyPrice
from src.market_data.providers.yahoo_pilot import YahooPilotProvider
from src.market_data.validate_pilot import validate
from src.market_data.yahoo_pilot import FIELDS, read_manifest, run_pilot


class FrameTicker:
    def __init__(self, frame, calls):
        self.frame = frame
        self.calls = calls

    def history(self, **kwargs):
        self.calls.append(kwargs)
        return self.frame


def test_yfinance_contract_and_adjustment_fields():
    calls = []
    frame = pd.DataFrame([{"Open": 10, "High": 12, "Low": 9, "Close": 11,
        "Adj Close": 10.5, "Volume": 1000, "Dividends": .2, "Stock Splits": 2}],
        index=pd.to_datetime(["2020-01-02"]))
    provider = YahooPilotProvider(lambda symbol: FrameTicker(frame, calls))
    price = provider.get_daily_prices("ABC", "2020-01-02", "2020-01-02")[0]
    assert calls[0]["end"] == "2020-01-03"  # yfinance end is exclusive
    assert calls[0]["auto_adjust"] is False and calls[0]["actions"] is True
    assert (price.open, price.close, price.adjusted_close, price.volume) == (10, 11, 10.5, 1000)
    assert (price.dividend_cash, price.split_factor, price.adjusted_open) == (.2, 2, None)
    assert price.adjustment_basis == "YAHOO_ADJ_CLOSE_VENDOR"
    with pytest.raises(ValueError, match="missing columns"):
        YahooPilotProvider(lambda symbol: FrameTicker(frame.drop(columns=["Stock Splits"]), [])).get_daily_prices(
            "ABC", "2020-01-02", "2020-01-02")


class SyntheticProvider:
    name = "yahoo"

    def __init__(self, prices):
        self.prices = prices
        self.calls = 0

    def get_daily_prices(self, symbol, start, end):
        self.calls += 1
        return self.prices


class RecordingProvider(SyntheticProvider):
    def __init__(self, prices):
        super().__init__(prices)
        self.windows = []

    def get_daily_prices(self, symbol, start, end):
        self.windows.append((symbol, start, end))
        return super().get_daily_prices(symbol, start, end)


def source_and_manifest(root):
    source = root / "source.db"
    conn = sqlite3.connect(source)
    conn.execute("""CREATE TABLE security_identifier_mapping (
        mapping_id TEXT PRIMARY KEY, issuer_cik TEXT, market_data_symbol TEXT,
        ticker_clean TEXT, valid_from TEXT, valid_to TEXT,
        is_ambiguous INTEGER, is_reused_ticker INTEGER)""")
    conn.execute("INSERT INTO security_identifier_mapping VALUES (?,?,?,?,?,?,?,?)",
        ("m1", "0001", "ABC", "ABC", "2020-01-01", "2020-01-31", 0, 0))
    conn.commit()
    conn.close()
    manifest = root / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow(dict(security_id="m1", issuer_cik="0001", ticker="ABC",
            provider_ticker="ABC", company_name_hint="ABC Inc", valid_from="2020-01-01",
            valid_to="2020-01-31", mapping_confidence="HIGH",
            known_ticker_reuse_issuer_count="1", ticker_change_flag="0",
            inactive_observation_flag="0", review_status="PENDING", identity_evidence=""))
    return source, manifest


def test_review_gate_isolation_idempotence_and_missing_data():
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
        root = Path(tmp)
        source, manifest = source_and_manifest(root)
        with pytest.raises(ValueError, match="reviewed identities"):
            read_manifest(manifest, source)
        manifest.write_text(manifest.read_text().replace("PENDING", "APPROVED").replace(
            "APPROVED,\n", "APPROVED,independent filing and exchange record\n"))
        price = DailyPrice("ABC", "2020-01-02", 10, 12, 9, 11, None, None, None,
            10.5, 1000, "yahoo", split_factor=2, dividend_cash=.2,
            adjustment_basis="YAHOO_ADJ_CLOSE_VENDOR")
        provider = SyntheticProvider([price])
        pilot_db = root / "pilot.db"
        stats = run_pilot(manifest, source, pilot_db, provider,
            "2020-01-02", "2020-01-02", delay=0)
        assert stats["complete"] == 1 and stats["history_calls"] == 1
        conn = sqlite3.connect(pilot_db)
        try:
            assert conn.execute("SELECT close,adj_close,volume,split_factor,dividend_cash FROM pilot_prices").fetchone() == (
                11, 10.5, 1000, 2, .2)
        finally:
            conn.close()
        assert run_pilot(manifest, source, pilot_db, provider,
            "2020-01-02", "2020-01-02", delay=0)["skipped"] == 1
        assert provider.calls == 1
        checks = validate(pilot_db, root / "qa")
        assert not any(c["severity"] == "FAIL" for c in checks)
        with pytest.raises(ValueError, match="production database"):
            run_pilot(manifest, source, source, provider, "2020-01-02", "2020-01-02", delay=0)
        empty = SyntheticProvider([])
        empty_db = root / "empty.db"
        assert run_pilot(manifest, source, empty_db, empty,
            "2020-01-02", "2020-01-02", delay=0)["no_data"] == 1
        checks = validate(empty_db, root / "empty_qa")
        assert any(c["check"] == "missing_price_series" and c["severity"] == "FAIL"
                   for c in checks)


def test_five_security_cap_and_verified_interval_clipping():
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
        root = Path(tmp)
        source, manifest = source_and_manifest(root)
        conn = sqlite3.connect(source)
        try:
            conn.executemany("INSERT INTO security_identifier_mapping VALUES (?,?,?,?,?,?,?,?)",
                [(f"m{i}", f"000{i}", f"S{i}", f"S{i}", "2020-01-01",
                  "2020-01-31", 0, 0) for i in range(2, 6)])
            conn.commit()
        finally:
            conn.close()
        with manifest.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["review_status"] = "APPROVED"
        rows[0]["identity_evidence"] = "independent issuer filing"
        for i in range(2, 6):
            rows.append({**rows[0], "security_id": f"m{i}", "issuer_cik": f"000{i}",
                         "ticker": f"S{i}", "provider_ticker": f"S{i}"})
        with manifest.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        price = DailyPrice("ABC", "2020-01-02", 10, 12, 9, 11, None, None, None,
            10.5, 1000, "yahoo", split_factor=0, dividend_cash=0,
            adjustment_basis="YAHOO_ADJ_CLOSE_VENDOR")
        provider = RecordingProvider([price])
        stats = run_pilot(manifest, source, root / "five.db", provider,
                          "2019-01-01", "2021-01-01", retry_count=0, delay=0)
        assert stats["history_calls"] == 5  # one per approved row, not a fixed ten
        assert len(provider.windows) == 5
        assert all(start == "2020-01-01" and end == "2020-01-31"
                   for _, start, end in provider.windows)
        with closing(sqlite3.connect(root / "five.db")) as conn:
            assert conn.execute("SELECT COUNT(*) FROM pilot_prices").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM pilot_prices WHERE trading_date NOT BETWEEN ? AND ?",
                ("2020-01-01", "2020-01-31")).fetchone()[0] == 0
        outside = RecordingProvider([])
        stats = run_pilot(manifest, source, root / "outside.db", outside,
                          "2021-02-01", "2021-02-28", delay=0)
        assert stats["history_calls"] == 0 and stats["skipped"] == 5
        assert outside.windows == []
