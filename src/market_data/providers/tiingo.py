"""Tiingo provider placeholder for future credentialed integration."""

from __future__ import annotations

from src.market_data.market_data_models import DailyPrice
from src.market_data.providers.base import MarketDataProvider


class TiingoProvider(MarketDataProvider):
    name = "tiingo"

    def get_daily_prices(self, symbol: str, start_date: str, end_date: str) -> list[DailyPrice]:
        raise NotImplementedError("Tiingo integration requires explicit API configuration.")
