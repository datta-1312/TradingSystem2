"""Technical building blocks used by Stage 1 and Stage 2.

Inputs are OHLCV DataFrames with columns ``open, high, low, close, volume`` and
a DatetimeIndex (daily: one row per NSE session). Every lookback is a number of
bars, i.e. NSE trading days on daily data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

OHLCV = ["open", "high", "low", "close", "volume"]


# ------------------------------------------------------------------ moving averages / volatility
def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, seeded with the first value (no look-ahead)."""
    return series.ewm(span=period, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    )
    tr = ranges.max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr


def true_range_pct(df: pd.DataFrame) -> pd.Series:
    """True range as % of the previous close (comparable across price levels)."""
    ref = df["close"].shift(1).fillna(df["close"])
    return true_range(df) / ref * 100.0


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's average true range."""
    return true_range(df).ewm(alpha=1.0 / period, adjust=False).mean()


def slope_pct_per_day(series: pd.Series, days: int) -> pd.Series:
    """Average % change per bar over ``days`` bars (EMA slope)."""
    return (series / series.shift(days) - 1.0) * 100.0 / days


# ------------------------------------------------------------------ swing points
@dataclass(frozen=True)
class Swing:
    pos: int  # integer position in the frame
    date: pd.Timestamp
    price: float
    kind: Literal["high", "low"]
    confirmed: bool


def swing_points(
    df: pd.DataFrame, lookaround: int, kind: Literal["high", "low"], include_provisional: bool = False
) -> list[Swing]:
    """Fractal swing points.

    A swing low is a bar whose low is strictly below the ``lookaround`` bars
    before it and not above the ``lookaround`` bars after it (mirror for highs).
    A swing is *confirmed* once ``lookaround`` bars exist on its right. With
    ``include_provisional`` the latest candidate in the last ``lookaround`` bars
    is also returned (``confirmed=False``) if at least one bar follows it.
    """
    col = "low" if kind == "low" else "high"
    values = df[col].to_numpy(dtype=float)
    n = len(values)
    k = lookaround
    out: list[Swing] = []
    better = (lambda a, b: a < b) if kind == "low" else (lambda a, b: a > b)
    not_worse = (lambda a, b: a <= b) if kind == "low" else (lambda a, b: a >= b)
    for i in range(k, n):
        left = values[i - k : i]
        right = values[i + 1 : i + k + 1]
        confirmed = len(right) == k
        if not confirmed and (not include_provisional or len(right) == 0):
            continue
        extreme_left = left.min() if kind == "low" else left.max()
        extreme_right = right.min() if kind == "low" else right.max()
        if better(values[i], extreme_left) and not_worse(values[i], extreme_right):
            out.append(Swing(i, df.index[i], float(values[i]), kind, confirmed))
    return out


# ------------------------------------------------------------------ gaps
@dataclass
class Gap:
    date: pd.Timestamp
    direction: Literal["up", "down"]
    original_bottom: float
    original_top: float
    bottom: float  # unfilled portion
    top: float
    filled: bool
    filled_date: pd.Timestamp | None

    @property
    def size_pct(self) -> float:
        return (self.original_top / self.original_bottom - 1.0) * 100.0


def find_gaps(df: pd.DataFrame, min_gap_pct: float, lookback: int) -> list[Gap]:
    """Price gaps in the last ``lookback`` bars and how much of each is still open.

    Gap up on day t: low[t] > high[t-1]. It is partly filled when a later low
    trades into it and fully filled once a low reaches high[t-1]. Gap downs mirror this.
    """
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    n = len(df)
    start = max(1, n - lookback)
    gaps: list[Gap] = []
    for t in range(start, n):
        if lows[t] > highs[t - 1] and (lows[t] / highs[t - 1] - 1) * 100 >= min_gap_pct:
            bottom, top = highs[t - 1], lows[t]
            g = Gap(df.index[t], "up", bottom, top, bottom, top, False, None)
            for j in range(t + 1, n):
                if lows[j] <= bottom:
                    g.filled, g.filled_date, g.top = True, df.index[j], bottom
                    break
                g.top = min(g.top, lows[j])
            gaps.append(g)
        elif highs[t] < lows[t - 1] and (lows[t - 1] / highs[t] - 1) * 100 >= min_gap_pct:
            bottom, top = highs[t], lows[t - 1]
            g = Gap(df.index[t], "down", bottom, top, bottom, top, False, None)
            for j in range(t + 1, n):
                if highs[j] >= top:
                    g.filled, g.filled_date, g.bottom = True, df.index[j], top
                    break
                g.bottom = max(g.bottom, highs[j])
            gaps.append(g)
    return gaps


# ------------------------------------------------------------------ anchored VWAP
def typical_price(df: pd.DataFrame, how: Literal["hlc3", "ohlc4", "close"] = "hlc3") -> pd.Series:
    if how == "hlc3":
        return (df["high"] + df["low"] + df["close"]) / 3.0
    if how == "ohlc4":
        return (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    return df["close"].astype(float)


def avwap(df: pd.DataFrame, anchor_pos: int, how: Literal["hlc3", "ohlc4", "close"] = "hlc3") -> pd.Series:
    """Volume-weighted average price from the anchor bar (inclusive) onward; NaN before it."""
    tp = typical_price(df, how)
    vol = df["volume"].astype(float)
    pv = (tp * vol).iloc[anchor_pos:].cumsum()
    cv = vol.iloc[anchor_pos:].cumsum()
    out = pd.Series(np.nan, index=df.index, dtype=float)
    vals = (pv / cv.replace(0.0, np.nan)).fillna(tp.iloc[anchor_pos:])
    out.iloc[anchor_pos:] = vals.to_numpy()
    return out


def avwap_state(
    close: pd.Series, low: pd.Series, vwap: pd.Series, near_pct: float
) -> dict[str, object]:
    """Has price reclaimed the AVWAP, and is it now pulling back toward it?

    States: ``below`` (latest close under the AVWAP and no reclaim),
    ``reclaimed`` (crossed back above), ``pullback`` (reclaimed and the latest low
    is within ``near_pct`` % above the AVWAP), ``lost`` (closed back below after a reclaim).
    """
    valid = vwap.notna()
    c, lo, v = close[valid], low[valid], vwap[valid]
    if c.empty:
        return {"state": "n/a", "reclaim_date": None, "distance_pct": None}
    above = c > v
    reclaim_date = None  # latest close that crossed from below to above
    ever_reclaimed = False
    prev_above: bool | None = None
    for ts, is_above in above.items():
        if prev_above is False and is_above:
            reclaim_date, ever_reclaimed = ts, True
        prev_above = bool(is_above)
    last_close, last_low, last_v = float(c.iloc[-1]), float(lo.iloc[-1]), float(v.iloc[-1])
    distance = (last_close / last_v - 1.0) * 100.0
    if not above.iloc[-1]:
        state = "lost" if ever_reclaimed else "below"
    elif reclaim_date is None:
        state = "above"  # has not closed below since the anchor
    elif (last_low / last_v - 1.0) * 100.0 <= near_pct:
        state = "pullback"
    else:
        state = "reclaimed"
    return {"state": state, "reclaim_date": reclaim_date, "distance_pct": distance}


# ------------------------------------------------------------------ resampling
def resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Weekly bars (Monday–Sunday weeks) labelled with the week's last session date."""
    if df.empty:
        return df.copy()
    grouper = pd.Grouper(freq="W-SUN")
    agg = df.groupby(grouper).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum")
    )
    last_dates = df.index.to_series().groupby(grouper).max()
    agg.index = pd.DatetimeIndex(last_dates.reindex(agg.index).to_numpy())
    return agg.dropna(subset=["close"])


def check_ohlcv(df: pd.DataFrame) -> None:
    missing = [c for c in OHLCV if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV frame is missing columns {missing}")
    if not df.index.is_monotonic_increasing:
        raise ValueError("OHLCV index must be sorted ascending")
