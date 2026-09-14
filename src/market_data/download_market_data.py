"""Resumable market-data downloader.

No provider is chosen implicitly. Tests use mock providers; live downloads
require an explicitly configured provider implementation.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import yaml

from src.market_data.market_data_repository import MarketDataRepository
from src.market_data.providers.base import MarketDataProvider
from src.market_data.providers.yahoo import YahooProvider
from src.utils.db import PROJECT_ROOT, get_connection

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "insider_research.db"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "market_data.yaml"


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    if not path.exists():
        return {
            "provider": "none",
            "retry_count": 2,
            "rate_limit_seconds": 0.0,
            "batch_size": 100,
            "forward_buffer_days": 370,
        }
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve_provider(name: str) -> MarketDataProvider:
    if name == "yahoo":
        return YahooProvider()
    raise ValueError(f"No configured market-data provider adapter for {name!r}")


def required_mvp_window(conn) -> tuple[str | None, str | None]:
    row = conn.execute(
        """
        SELECT MIN(filing_date), MAX(filing_date)
        FROM transaction_market_mapping tm
        JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
        WHERE t.include_mvp_alpha = 1 AND tm.is_mapped = 1
        """
    ).fetchone()
    return row[0], row[1]


def required_symbols(conn, limit: int | None = None) -> list[str]:
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    rows = conn.execute(
        f"""
        SELECT DISTINCT market_data_symbol
        FROM transaction_market_mapping tm
        JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
        WHERE t.include_mvp_alpha = 1
          AND tm.is_mapped = 1
          AND market_data_symbol IS NOT NULL
        ORDER BY market_data_symbol
        {limit_sql}
        """
    ).fetchall()
    return [row[0] for row in rows]


def download_symbols(
    *,
    conn,
    provider: MarketDataProvider,
    symbols: list[str],
    start_date: str,
    end_date: str,
    retry_count: int = 2,
    rate_limit_seconds: float = 0.0,
) -> dict[str, int]:
    repo = MarketDataRepository(conn)
    stats = {"requested": 0, "complete": 0, "failed": 0, "no_data": 0, "skipped": 0}
    for symbol in symbols:
        stats["requested"] += 1
        if repo.already_complete(symbol, provider.name, start_date, end_date):
            stats["skipped"] += 1
            continue
        repo.upsert_download_status(
            symbol=symbol,
            provider=provider.name,
            requested_start=start_date,
            requested_end=end_date,
            status="IN_PROGRESS",
        )
        last_error = None
        prices = []
        for _attempt in range(retry_count + 1):
            try:
                prices = provider.get_daily_prices(symbol, start_date, end_date)
                break
            except Exception as exc:  # provider errors are recorded, not raised per symbol
                last_error = str(exc)
        if prices:
            repo.upsert_prices(prices)
            dates = [p.trading_date for p in prices]
            repo.upsert_download_status(
                symbol=symbol,
                provider=provider.name,
                requested_start=start_date,
                requested_end=end_date,
                downloaded_start=min(dates),
                downloaded_end=max(dates),
                row_count=len(prices),
                status="COMPLETE",
            )
            stats["complete"] += 1
        elif last_error:
            repo.upsert_download_status(
                symbol=symbol,
                provider=provider.name,
                requested_start=start_date,
                requested_end=end_date,
                row_count=0,
                status="FAILED",
                last_error=last_error,
            )
            stats["failed"] += 1
        else:
            repo.upsert_download_status(
                symbol=symbol,
                provider=provider.name,
                requested_start=start_date,
                requested_end=end_date,
                row_count=0,
                status="NO_DATA",
            )
            stats["no_data"] += 1
        if rate_limit_seconds:
            time.sleep(rate_limit_seconds)
    return stats


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download daily market prices for mapped MVP symbols.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--limit-symbols", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stdout,
    )
    config = load_config(args.config)
    provider_name = args.provider or config.get("provider")
    if not provider_name or provider_name == "none":
        logger.error("No market-data provider configured; infrastructure only.")
        return 1
    provider = resolve_provider(provider_name)
    conn = get_connection(args.db_path)
    try:
        start, end = required_mvp_window(conn)
        start = args.start_date or start
        end = args.end_date or end
        if not start or not end:
            logger.error("No mapped MVP date window available.")
            return 1
        symbols = required_symbols(conn, args.limit_symbols)
        stats = download_symbols(
            conn=conn,
            provider=provider,
            symbols=symbols,
            start_date=start,
            end_date=end,
            retry_count=int(config.get("retry_count", 2)),
            rate_limit_seconds=float(config.get("rate_limit_seconds", 0.0)),
        )
        logger.info("download stats=%s", stats)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
