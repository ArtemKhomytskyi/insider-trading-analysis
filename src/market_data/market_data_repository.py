"""SQLite repository for market-data tables."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from src.market_data.market_data_models import DailyPrice
from src.utils.db import execute_sql_file, PROJECT_ROOT

SCHEMA_PATH = PROJECT_ROOT / "sql" / "004_create_market_data_tables.sql"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MarketDataRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        execute_sql_file(conn, SCHEMA_PATH)

    def upsert_prices(self, prices: list[DailyPrice]) -> int:
        if not prices:
            return 0
        downloaded_at = utc_now()
        rows = [
            (
                p.market_data_symbol,
                p.trading_date,
                p.open,
                p.high,
                p.low,
                p.close,
                p.adjusted_open,
                p.adjusted_high,
                p.adjusted_low,
                p.adjusted_close,
                p.volume,
                p.provider,
                p.currency,
                p.exchange,
                p.is_adjusted,
                downloaded_at,
            )
            for p in prices
        ]
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO market_prices_daily (
                market_data_symbol, trading_date, open, high, low, close,
                adjusted_open, adjusted_high, adjusted_low, adjusted_close,
                volume, provider, currency, exchange, is_adjusted, downloaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def upsert_download_status(
        self,
        *,
        symbol: str,
        provider: str,
        requested_start: str,
        requested_end: str,
        status: str,
        row_count: int = 0,
        downloaded_start: str | None = None,
        downloaded_end: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = utc_now()
        existing = self.conn.execute(
            """
            SELECT attempt_count FROM market_data_download_status
            WHERE market_data_symbol = ? AND provider = ?
              AND requested_start = ? AND requested_end = ?
            """,
            (symbol, provider, requested_start, requested_end),
        ).fetchone()
        attempts = int(existing[0]) + 1 if existing else 1
        completed = now if status in {"COMPLETE", "FAILED", "NO_DATA", "PARTIAL"} else None
        self.conn.execute(
            """
            INSERT OR REPLACE INTO market_data_download_status (
                market_data_symbol, provider, requested_start, requested_end,
                downloaded_start, downloaded_end, row_count, status, attempt_count,
                last_error, last_attempt_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                provider,
                requested_start,
                requested_end,
                downloaded_start,
                downloaded_end,
                row_count,
                status,
                attempts,
                last_error,
                now,
                completed,
            ),
        )
        self.conn.commit()

    def already_complete(
        self,
        symbol: str,
        provider: str,
        requested_start: str,
        requested_end: str,
    ) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM market_data_download_status
            WHERE market_data_symbol = ? AND provider = ?
              AND requested_start = ? AND requested_end = ?
              AND status = 'COMPLETE'
            """,
            (symbol, provider, requested_start, requested_end),
        ).fetchone()
        return row is not None
