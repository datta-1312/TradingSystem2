"""Deterministic synthetic market data for tests and the demo.

Everything here is clearly fake: symbols end in ``.SYN``-style names you will
not find on NSE. The generator draws a price path through waypoints you choose
(so swing lows, contractions and EMA reclaims land exactly where a test needs
them) and builds OHLCV bars around it on real NSE session dates.

It never stands in for real data: the CLI labels every synthetic run as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from nseswing.core.calendar import NSECalendar
from nseswing.core.timeutil import IST


@dataclass
class Profile:
    """Waypoints are (bar index, close). ``range_pct`` maps a bar index to its high-low range in %."""

    symbol: str
    waypoints: list[tuple[int, float]]
    range_pct: list[tuple[int, float]] = field(default_factory=lambda: [(0, 2.0)])
    volume: float = 800_000.0
    noise_pct: float = 0.25
    industry: str = "Synthetic Industry"
    seed: int = 7


def _interp(points: list[tuple[int, float]], n: int) -> np.ndarray:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return np.interp(np.arange(n), xs, ys)


def daily_from_profile(p: Profile, dates: list[date]) -> pd.DataFrame:
    n = len(dates)
    rng = np.random.RandomState(p.seed)
    centre = _interp(p.waypoints, n)
    closes = centre * (1 + rng.normal(0, p.noise_pct / 100.0, n))
    # keep waypoints exact so tests can rely on them
    for i, c in p.waypoints:
        if 0 <= i < n:
            closes[i] = c
    rng_pct = _interp(p.range_pct, n) / 100.0
    rows = []
    prev_close = closes[0]
    for i in range(n):
        c = closes[i]
        o = prev_close + (c - prev_close) * 0.35 if i else c
        half = c * rng_pct[i] / 2.0
        hi = max(o, c) + half * 0.6
        lo = min(o, c) - half * 0.6
        # waypoint bars are exact turning points: make the low/high the extreme there
        rows.append([o, hi, lo, c, p.volume * (1 + 0.2 * rng.rand())])
        prev_close = c
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(pd.to_datetime(dates)), columns=["open", "high", "low", "close", "volume"])
    df.index.name = "date"
    return df.round({"open": 2, "high": 2, "low": 2, "close": 2, "volume": 0})


def session_dates(calendar: NSECalendar, end: date, n: int) -> list[date]:
    out: list[date] = []
    d = end
    while len(out) < n:
        if calendar.is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


# ------------------------------------------------------------------ stock archetypes (300 bars)
def _base_waypoints(b: float, second_low: float) -> list[tuple[int, float]]:
    """Flat, advance to a peak at bar 262, pullback low at 272, bounce at 280, second low at 288, then up."""
    return [
        (0, b), (140, b * 1.02), (255, b * 1.60), (262, b * 1.66), (272, b * 1.50), (280, b * 1.61),
        (288, b * second_low), (295, b * 1.585), (299, b * 1.60),
    ]


def leader(symbol: str, base: float = 300.0, industry: str = "Capital Goods", seed: int = 1, volume: float = 900_000.0) -> Profile:
    """Advance +60%, then a 37-session base: higher low, contracting range, EMA reclaim (passes Stage 1)."""
    return Profile(
        symbol,
        _base_waypoints(base, 1.545),
        range_pct=[(0, 2.2), (255, 3.4), (262, 3.6), (272, 3.0), (285, 1.8), (299, 1.3)],
        volume=volume,
        industry=industry,
        seed=seed,
        noise_pct=0.1,
    )


orderly_leader = leader


def extended(symbol: str, base: float = 200.0, seed: int = 2, industry: str = "Capital Goods") -> Profile:
    """Straight up into the last bar: no pullback on the weekly chart."""
    return Profile(symbol, [(0, base), (150, base * 1.05), (299, base * 1.9)], range_pct=[(0, 2.5)], seed=seed, industry=industry)


def broken(symbol: str, base: float = 400.0, seed: int = 3, industry: str = "Chemicals") -> Profile:
    """Advance, then lower highs and lower lows below falling averages (disqualified)."""
    b = base
    return Profile(
        symbol,
        [(0, b), (150, b * 1.5), (190, b * 1.7), (215, b * 1.45), (228, b * 1.58), (245, b * 1.32), (258, b * 1.42), (275, b * 1.18), (283, b * 1.24), (299, b * 1.08)],
        range_pct=[(0, 2.5)],
        seed=seed,
        industry=industry,
        noise_pct=0.1,
    )


def lower_low_base(symbol: str, base: float = 250.0, seed: int = 4, industry: str = "Chemicals") -> Profile:
    """Advance and pullback, but the second pullback low undercuts the first (fails 7a)."""
    p = leader(symbol, base=base, industry=industry, seed=seed)
    p.waypoints = _base_waypoints(base, 1.46)
    return p


def laggard(symbol: str, base: float = 150.0, seed: int = 5, industry: str = "FMCG") -> Profile:
    return Profile(symbol, [(0, base), (299, base * 1.03)], range_pct=[(0, 1.6)], seed=seed, industry=industry)


def illiquid(symbol: str, seed: int = 6) -> Profile:
    p = leader(symbol, base=120.0, industry="Textiles", seed=seed)
    p.volume = 9_000.0
    return p


def loose(symbol: str, base: float = 500.0, seed: int = 8, industry: str = "Realty") -> Profile:
    """Advance and higher lows, but the range keeps widening (fails 7b)."""
    p = leader(symbol, base=base, industry=industry, seed=seed)
    p.range_pct = [(0, 1.5), (262, 1.6), (272, 2.5), (288, 4.0), (299, 5.0)]
    return p


def make_universe(calendar: NSECalendar, end: date, n_bars: int = 300) -> tuple[dict[str, pd.DataFrame], dict[str, Profile]]:
    """A small universe: three Capital Goods leaders (a hot group) plus failing archetypes."""
    profiles = [
        leader("ALPHA.SYN", base=300.0, seed=11),
        leader("BRAVO.SYN", base=820.0, seed=12),
        leader("CHARLIE.SYN", base=1450.0, seed=13),
        extended("DELTA.SYN"),
        broken("ECHO.SYN"),
        lower_low_base("FOXTROT.SYN"),
        laggard("GOLF.SYN"),
        illiquid("HOTEL.SYN"),
        loose("INDIA.SYN"),
    ]
    for i in range(12):  # flat filler so ranks have a population
        profiles.append(laggard(f"FILL{i:02d}.SYN", base=100.0 + 37 * i, seed=100 + i, industry="Filler"))
    dates = session_dates(calendar, end, n_bars)
    data = {p.symbol: daily_from_profile(p, dates) for p in profiles}
    return data, {p.symbol: p for p in profiles}


# ------------------------------------------------------------------ intraday helpers
def intraday_path(
    day: date,
    points: list[tuple[str, float]],
    open_time: time = time(9, 15),
    close_time: time = time(15, 30),
    volume_per_min: float = 4000.0,
    wiggle: float = 0.0006,
    seed: int = 3,
) -> pd.DataFrame:
    """1-minute bars through (HH:MM, price) waypoints; each bar spans its move ± a small wiggle."""
    start = datetime.combine(day, open_time, tzinfo=IST)
    n = int((datetime.combine(day, close_time) - datetime.combine(day, open_time)).total_seconds() // 60)
    xs = [int((datetime.combine(day, time.fromisoformat(t)) - datetime.combine(day, open_time)).total_seconds() // 60) for t, _ in points]
    ys = [p for _, p in points]
    path = np.interp(np.arange(n + 1), xs, ys)
    rng = np.random.RandomState(seed)
    rows = []
    for i in range(n):
        o, c = path[i], path[i + 1]
        hi = max(o, c) * (1 + wiggle * rng.rand())
        lo = min(o, c) * (1 - wiggle * rng.rand())
        rows.append([o, hi, lo, c, volume_per_min * (0.6 + rng.rand())])
    idx = pd.date_range(start, periods=n, freq="1min")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"]).round(2)
