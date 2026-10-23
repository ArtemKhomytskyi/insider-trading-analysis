"""Tiingo EOD adapter. Uses only the documented historical prices endpoint."""

from __future__ import annotations

import json
import os
from datetime import date
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from src.market_data.market_data_models import DailyPrice
from src.market_data.providers.base import MarketDataProvider


class TiingoProvider(MarketDataProvider):
    name = "tiingo"

    def _fetch(self, symbol: str, suffix: str = ""):
        key = os.environ.get("TIINGO_API_KEY")
        if not key:
            raise RuntimeError("TIINGO_API_KEY is not set")
        url = f"https://api.tiingo.com/tiingo/daily/{quote(symbol, safe='')}{suffix}"
        request = Request(url, headers={"Authorization": f"Token {key}", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                raise UnknownTicker(f"Tiingo does not recognize {symbol}") from exc
            raise

    def get_metadata(self, symbol: str) -> dict:
        payload = self._fetch(symbol)
        if not isinstance(payload, dict) or not payload.get("ticker") or not payload.get("name"):
            raise ValueError("Incomplete Tiingo metadata response")
        return payload

    def get_daily_prices(self, symbol: str, start_date: str, end_date: str) -> list[DailyPrice]:
        payload = self._fetch(symbol, "/prices?" +
            urlencode({"startDate": start_date, "endDate": end_date, "format": "json"}))
        if not isinstance(payload, list):
            raise ValueError("Unexpected Tiingo response shape")
        result = []
        seen_dates: set[str] = set()
        for row in payload:
            required = ("date", "open", "high", "low", "close", "volume",
                        "adjOpen", "adjHigh", "adjLow", "adjClose", "adjVolume")
            if not isinstance(row, dict) or any(row.get(field) is None for field in required):
                raise ValueError("Incomplete Tiingo daily price response")
            trading_date = row["date"][:10]
            date.fromisoformat(trading_date)
            if trading_date in seen_dates:
                raise ValueError("Duplicate date in Tiingo daily price response")
            seen_dates.add(trading_date)
            result.append(DailyPrice(
                market_data_symbol=symbol, trading_date=trading_date,
                open=row.get("open"), high=row.get("high"), low=row.get("low"),
                close=row.get("close"), adjusted_open=row.get("adjOpen"),
                adjusted_high=row.get("adjHigh"), adjusted_low=row.get("adjLow"),
                adjusted_close=row["adjClose"], volume=row.get("volume"),
                adjusted_volume=row.get("adjVolume"), split_factor=row.get("splitFactor"),
                dividend_cash=row.get("divCash"), provider=self.name,
                adjustment_basis="TIINGO_DIVIDEND_AND_SPLIT_ADJUSTED",
            ))
        return result


class UnknownTicker(ValueError):
    """The vendor has no record of the requested historical symbol."""
