"""Provider abstraction for daily market data."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.market_data.market_data_models import DailyPrice


class MarketDataProvider(ABC):
    name: str

    @abstractmethod
    def get_daily_prices(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> list[DailyPrice]:
        """Return adjusted daily prices for symbol/date range."""
