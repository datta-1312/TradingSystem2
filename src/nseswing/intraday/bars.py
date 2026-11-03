"""Intraday bars aligned to the session open (09:15 IST, or a special session's open).

* :func:`resample_bars` turns 1-minute candles into 5- or 15-minute bars.
* :class:`BarBuilder` turns live ticks (LTP polls) into bars, using the
  cumulative day volume from the quote to get each bar's volume.

Bars are labelled by their start time: the 09:15 5-min bar covers 09:15:00–09:19:59.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

import pandas as pd

from nseswing.core.timeutil import IST, to_ist
from nseswing.data.base import normalize_intraday


@dataclass(frozen=True)
class Bar:
    start: pd.Timestamp
    end: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_row(cls, ts: pd.Timestamp, row: pd.Series, minutes: int) -> "Bar":
        start = to_ist(ts)
        return cls(start, start + pd.Timedelta(minutes=minutes), float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row["volume"]))


def resample_bars(df: pd.DataFrame, minutes: int, opens: dict[date, datetime] | None = None) -> pd.DataFrame:
    """Resample 1-min OHLCV to ``minutes``; each day is aligned to its session open."""
    df = normalize_intraday(df)
    if minutes == 1 or df.empty:
        return df
    frames = []
    for day, chunk in df.groupby(df.index.date):
        origin = (opens or {}).get(day) or datetime.combine(day, time(9, 15), tzinfo=IST)
        agg = chunk.resample(f"{minutes}min", origin=pd.Timestamp(origin), label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        )
        frames.append(agg.dropna(subset=["open"]))
    return normalize_intraday(pd.concat(frames))


def bars_from_frame(df: pd.DataFrame, minutes: int) -> list[Bar]:
    return [Bar.from_row(ts, row, minutes) for ts, row in df.iterrows()]


class BarBuilder:
    """Builds bars from ticks. ``on_tick`` returns the bars completed by that tick."""

    def __init__(self, minutes: int, session_open: datetime):
        self.minutes = minutes
        self.session_open = to_ist(session_open)
        self._cur: Bar | None = None
        self._last_cum_vol: float | None = None

    def _bucket_start(self, ts: pd.Timestamp) -> pd.Timestamp:
        elapsed = (ts - self.session_open) // pd.Timedelta(minutes=self.minutes)
        return self.session_open + pd.Timedelta(minutes=self.minutes) * int(elapsed)

    def on_tick(self, ts: datetime | pd.Timestamp, price: float, cum_volume: float | None = None) -> list[Bar]:
        t = to_ist(ts)
        if t < self.session_open:
            return []
        vol = 0.0
        if cum_volume is not None:
            if self._last_cum_vol is not None:
                vol = max(0.0, cum_volume - self._last_cum_vol)
            self._last_cum_vol = cum_volume
        done = self.flush(t)
        start = self._bucket_start(t)
        if self._cur is None:
            self._cur = Bar(start, start + pd.Timedelta(minutes=self.minutes), price, price, price, price, vol)
        else:
            c = self._cur
            self._cur = replace(c, high=max(c.high, price), low=min(c.low, price), close=price, volume=c.volume + vol)
        return done

    def flush(self, now: datetime | pd.Timestamp) -> list[Bar]:
        """Close the current bar if ``now`` is past its end."""
        t = to_ist(now)
        if self._cur is not None and t >= self._cur.end:
            done, self._cur = [self._cur], None
            return done
        return []

    @property
    def current(self) -> Bar | None:
        return self._cur


def minutes_between(a: datetime, b: datetime) -> float:
    return (to_ist(b) - to_ist(a)) / timedelta(minutes=1)
