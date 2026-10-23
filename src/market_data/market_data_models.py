"""Small data models for market-data providers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DailyPrice:
    market_data_symbol: str
    trading_date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adjusted_open: float | None
    adjusted_high: float | None
    adjusted_low: float | None
    adjusted_close: float
    volume: float | None
    provider: str
    currency: str | None = None
    exchange: str | None = None
    is_adjusted: int = 1
    adjusted_volume: float | None = None
    split_factor: float | None = None
    dividend_cash: float | None = None
    adjustment_basis: str = "PROVIDER_UNSPECIFIED"
