"""Tests for Phase 5.2 market-data infrastructure."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.analytics.validate_market_data import run_validation
from src.market_data.download_market_data import download_symbols
from src.market_data.market_data_models import DailyPrice
from src.market_data.market_data_repository import MarketDataRepository
from src.market_data.providers.base import MarketDataProvider


class MockProvider(MarketDataProvider):
    name = "mock"

    def __init__(self, *, fail_first: bool = False, no_data: bool = False) -> None:
        self.fail_first = fail_first
        self.no_data = no_data
        self.calls = 0

    def get_daily_prices(self, symbol: str, start_date: str, end_date: str) -> list[DailyPrice]:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("temporary failure")
        if self.no_data:
            return []
        return [
            DailyPrice(
                market_data_symbol=symbol,
                trading_date=start_date,
                open=10,
                high=12,
                low=9,
                close=11,
                adjusted_open=None,
                adjusted_high=None,
                adjusted_low=None,
                adjusted_close=10.5,
                volume=1000,
                provider=self.name,
            )
        ]


DDL = """
CREATE TABLE clean_insider_transactions (
    transaction_id TEXT PRIMARY KEY,
    include_mvp_alpha INTEGER,
    filing_date TEXT
);
"""


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / "market.db")
    connection.row_factory = sqlite3.Row
    connection.executescript(DDL)
    return connection


def test_adjusted_price_persistence_and_duplicate_prevention(conn: sqlite3.Connection) -> None:
    repo = MarketDataRepository(conn)
    price = DailyPrice("AAA", "2020-01-02", 10, 12, 9, 11, None, None, None, 10.5, 1000, "mock")
    assert repo.upsert_prices([price]) == 1
    assert repo.upsert_prices([price]) == 1
    assert conn.execute("SELECT COUNT(*) FROM market_prices_daily").fetchone()[0] == 1
    row = conn.execute("SELECT close, adjusted_close, provider FROM market_prices_daily").fetchone()
    assert row["close"] == 11
    assert row["adjusted_close"] == 10.5
    assert row["provider"] == "mock"


def test_failed_download_retry_then_complete(conn: sqlite3.Connection) -> None:
    provider = MockProvider(fail_first=True)
    stats = download_symbols(
        conn=conn,
        provider=provider,
        symbols=["AAA"],
        start_date="2020-01-02",
        end_date="2020-01-03",
        retry_count=1,
    )
    assert stats["complete"] == 1
    status = conn.execute("SELECT status, row_count FROM market_data_download_status").fetchone()
    assert status["status"] == "COMPLETE"
    assert status["row_count"] == 1


def test_no_data_handling(conn: sqlite3.Connection) -> None:
    provider = MockProvider(no_data=True)
    stats = download_symbols(
        conn=conn,
        provider=provider,
        symbols=["DELISTED"],
        start_date="2020-01-02",
        end_date="2020-01-03",
    )
    assert stats["no_data"] == 1
    assert conn.execute("SELECT status FROM market_data_download_status").fetchone()[0] == "NO_DATA"


def test_resumable_download_skips_complete(conn: sqlite3.Connection) -> None:
    provider = MockProvider()
    download_symbols(conn=conn, provider=provider, symbols=["AAA"], start_date="2020-01-02", end_date="2020-01-03")
    download_symbols(conn=conn, provider=provider, symbols=["AAA"], start_date="2020-01-02", end_date="2020-01-03")
    assert provider.calls == 1


def test_market_data_validation_flags_bad_ohlc(conn: sqlite3.Connection, tmp_path: Path) -> None:
    repo = MarketDataRepository(conn)
    repo.upsert_prices([DailyPrice("AAA", "2020-01-02", 20, 12, 9, 11, None, None, None, 10.5, 1000, "mock")])
    conn.execute("INSERT INTO clean_insider_transactions VALUES ('t1', 1, '2020-01-02')")
    conn.execute("INSERT INTO transaction_market_mapping (transaction_id, market_data_symbol, is_mapped) VALUES ('t1', 'AAA', 1)")
    conn.commit()
    summary = run_validation(conn, tmp_path)
    bad = summary.loc[summary["check_name"] == "invalid_ohlc_relationships"].iloc[0]
    assert bad["severity"] == "FAIL"


def test_incomplete_future_horizon_readiness_defaults_zero(conn: sqlite3.Connection, tmp_path: Path) -> None:
    repo = MarketDataRepository(conn)
    repo.upsert_prices([DailyPrice("AAA", "2020-01-02", 10, 12, 9, 11, None, None, None, 10.5, 1000, "mock")])
    conn.execute("INSERT INTO clean_insider_transactions VALUES ('t1', 1, '2020-01-02')")
    conn.execute("INSERT INTO transaction_market_mapping (transaction_id, market_data_symbol, is_mapped) VALUES ('t1', 'AAA', 1)")
    conn.commit()
    run_validation(conn, tmp_path)
    readiness = (tmp_path / "mvp_market_data_readiness.csv").read_text(encoding="utf-8")
    assert "has_252d_forward_coverage" in readiness
