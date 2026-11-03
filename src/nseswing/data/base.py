"""Data-provider interface and shared market-data types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

import pandas as pd

from nseswing.core.timeutil import IST

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass
class Quote:
    """A market snapshot. Circuit limits come straight from the broker when available."""

    symbol: str
    ltp: float
    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    upper_circuit: float | None = None
    lower_circuit: float | None = None
    timestamp: pd.Timestamp | None = None
    instrument_key: str | None = None


class DataProvider(Protocol):
    """Anything that can serve adjusted-or-raw candles and quotes for NSE symbols."""

    name: str

    def daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Daily OHLCV, index = session date (naive), ascending."""

    def intraday(self, symbol: str, day: date, interval_minutes: int = 1) -> pd.DataFrame:
        """Intraday OHLCV for one session, index = bar start time (tz-aware IST), ascending."""

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Latest snapshot per symbol."""


def normalize_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Float columns, naive date index, sorted, duplicates dropped (last wins)."""
    if df.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS, dtype=float)
    out = df[OHLCV_COLUMNS].astype(float).copy()
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_convert(IST).tz_localize(None)
    out.index = idx.normalize()
    out.index.name = "date"
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def normalize_intraday(df: pd.DataFrame) -> pd.DataFrame:
    """Float columns, tz-aware IST index, sorted, duplicates dropped."""
    if df.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS, dtype=float, index=pd.DatetimeIndex([], tz=IST))
    out = df[OHLCV_COLUMNS].astype(float).copy()
    idx = pd.DatetimeIndex(out.index)
    out.index = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    out.index.name = "datetime"
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out
