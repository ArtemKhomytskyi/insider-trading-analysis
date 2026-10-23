"""Yahoo Finance daily bars for the isolated research pilot only."""

from __future__ import annotations

from datetime import date, timedelta
import math

from src.market_data.market_data_models import DailyPrice


class YahooPilotProvider:
    name = "yahoo"

    def __init__(self, ticker_factory=None):
        self._ticker_factory = ticker_factory

    def get_daily_prices(self, symbol: str, start_date: str, end_date: str) -> list[DailyPrice]:
        factory = self._ticker_factory
        if factory is None:
            try:
                import yfinance as yf
            except ImportError as exc:
                raise RuntimeError("Install the project's yfinance dependency before a Yahoo pilot run") from exc
            factory = yf.Ticker
        # yfinance's end date is exclusive. The manifest interval is inclusive.
        end_exclusive = (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()
        frame = factory(symbol).history(
            start=start_date, end=end_exclusive, interval="1d",
            auto_adjust=False, actions=True, repair=False,
            keepna=False, raise_errors=True, timeout=30,
        )
        if frame is None or frame.empty:
            return []
        required = ("Open", "High", "Low", "Close", "Adj Close", "Volume",
                    "Dividends", "Stock Splits")
        missing = [field for field in required if field not in frame.columns]
        if missing:
            raise ValueError(f"Yahoo response missing columns: {', '.join(missing)}")
        prices = []
        seen_dates = set()
        for timestamp, row in frame.iterrows():
            trading_date = timestamp.date().isoformat()
            if trading_date in seen_dates:
                raise ValueError("Yahoo returned duplicate daily dates")
            seen_dates.add(trading_date)
            values = {field: float(row[field]) for field in required}
            if any(not math.isfinite(value) for value in values.values()):
                raise ValueError(f"Yahoo returned incomplete/nonfinite data on {trading_date}")
            prices.append(DailyPrice(
                market_data_symbol=symbol, trading_date=trading_date,
                open=values["Open"], high=values["High"], low=values["Low"],
                close=values["Close"], adjusted_open=None, adjusted_high=None,
                adjusted_low=None, adjusted_close=values["Adj Close"],
                volume=values["Volume"], provider=self.name,
                split_factor=values["Stock Splits"],
                dividend_cash=values["Dividends"],
                adjustment_basis="YAHOO_ADJ_CLOSE_VENDOR",
            ))
        return prices
