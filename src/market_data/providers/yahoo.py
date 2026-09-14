"""Yahoo-compatible provider placeholder.

This adapter is intentionally not wired to a live downloader by default. A
production run should explicitly configure and vet a provider before network
acquisition.
"""

from __future__ import annotations

from src.market_data.market_data_models import DailyPrice
from src.market_data.providers.base import MarketDataProvider


class YahooProvider(MarketDataProvider):
    name = "yahoo"

    def get_daily_prices(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> list[DailyPrice]:
        raise NotImplementedError(
            "YahooProvider is an architecture placeholder; configure an explicit "
            "provider implementation before live downloads."
        )
