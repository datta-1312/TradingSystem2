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


# ------------------------------------------------------------------ the reference replay scenario
@dataclass
class Scenario:
    """Synthetic data for one replay: daily history + 1-min bars for the replay day (+ later daily bars)."""

    symbol: str
    day: date
    daily: pd.DataFrame
    intraday: dict[date, pd.DataFrame]
    description: str
    zone_top: float = 0.0
    zone_bottom: float = 0.0


def _bar_row(o: float, h: float, lo: float, c: float, v: float) -> list[float]:
    return [round(o, 2), round(max(h, o, c), 2), round(min(lo, o, c), 2), round(c, 2), v]


def _intraday_frame(day: date, rows: dict[str, list[float]]) -> pd.DataFrame:
    idx = [pd.Timestamp(f"{day} {t}", tz=IST) for t in rows]
    return pd.DataFrame(list(rows.values()), index=pd.DatetimeIndex(idx), columns=["open", "high", "low", "close", "volume"])


def _history(settings, calendar: NSECalendar, day: date, symbol: str, seed: int):
    from nseswing.levels import best_zone, build_level_map
    from nseswing.screener import find_consolidation

    hist_dates = session_dates(calendar, calendar.previous_trading_day(day), 300)
    hist = daily_from_profile(leader(symbol, base=300.0, seed=seed), hist_dates)
    lm = build_level_map(symbol, hist, settings, find_consolidation(hist, settings))
    return hist, best_zone(lm.zones)


def _following_days(calendar: NSECalendar, day: date, closes: list[float], start_open: float) -> pd.DataFrame:
    rows, idx, prev = [], [], start_open
    d = day
    for c in closes:
        d = calendar.next_trading_day(d)
        o = prev * 1.002
        rows.append(_bar_row(o, max(o, c) * 1.006, min(o, c) * 0.995, c, 1_400_000.0))
        idx.append(pd.Timestamp(d))
        prev = c
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx), columns=["open", "high", "low", "close", "volume"])
    df.index.name = "date"
    return df


def reference_scenario(settings, calendar: NSECalendar, day: date, symbol: str = "REFDEMO.SYN", seed: int = 21) -> Scenario:
    """Gap up, pullback into a moving-average + AVWAP zone, a 1-min bar breaks the prior bar's high, rally to 3R.

    Following sessions reach 5R, stretch, then close below the 9 EMA (trailing exit).
    """
    hist, zone = _history(settings, calendar, day, symbol, seed)
    top, bottom = float(zone.top), float(zone.bottom)
    width = top - bottom
    prev_close = float(hist["close"].iloc[-1])
    open_px = round(prev_close * 1.012, 2)
    rng = np.random.RandomState(seed)

    rows: dict[str, list[float]] = {}
    t0 = datetime.combine(day, time(9, 15))
    # 09:15–09:39: drift from the gap-up open down toward the zone, staying above it
    approach = top * 1.002
    for i in range(25):
        a = open_px + (approach - open_px) * i / 25
        b = open_px + (approach - open_px) * (i + 1) / 25
        w = 0.0005 * rng.rand() * a
        rows[(t0 + timedelta(minutes=i)).strftime("%H:%M")] = _bar_row(a, max(a, b) + w, min(a, b) - w * 0.5, b, 9000 - 200 * i)
    flush_high = top * 1.001
    dip_low = bottom + 0.25 * width
    rows["09:40"] = _bar_row(approach, approach * 1.0004, top * 1.0002, top * 1.0006, 6500)
    rows["09:41"] = _bar_row(top * 1.0006, flush_high, dip_low, bottom + 0.4 * width, 14000)  # red flush into the zone
    reversal_high = flush_high * 1.0025
    rows["09:42"] = _bar_row(bottom + 0.4 * width, reversal_high, bottom + 0.38 * width, reversal_high * 0.9995, 16000)  # trades above 09:41's high
    entry = flush_high + 0.05
    r = entry - dip_low
    path = [("09:43", reversal_high), ("11:00", entry + 1.2 * r), ("12:30", entry + 2.2 * r), ("13:45", entry + 3.4 * r), ("15:29", entry + 3.1 * r)]
    t = datetime.combine(day, time(9, 43))
    end = datetime.combine(day, time(15, 30))
    k, prev = 0, reversal_high * 0.9995
    while t < end:
        hhmm = t.strftime("%H:%M")
        while k + 1 < len(path) and hhmm >= path[k + 1][0]:
            k += 1
        (ta, pa), (tb, pb) = path[k], path[min(k + 1, len(path) - 1)]
        ma = datetime.combine(day, time.fromisoformat(ta))
        mb = datetime.combine(day, time.fromisoformat(tb))
        frac = 1.0 if mb == ma else min(1.0, (t - ma) / (mb - ma))
        target = pa + (pb - pa) * frac
        c = target * (1 + rng.normal(0, 0.0004))
        w = 0.0004 * rng.rand() * c
        rows[hhmm] = _bar_row(prev, max(prev, c) + w, max(min(prev, c) - w, entry - 0.4 * r), c, 4000 + 3000 * rng.rand())
        prev = c
        t += timedelta(minutes=1)
    intraday = _intraday_frame(day, rows)

    day_bar = pd.DataFrame(
        [[intraday["open"].iloc[0], intraday["high"].max(), intraday["low"].min(), intraday["close"].iloc[-1], intraday["volume"].sum()]],
        index=pd.DatetimeIndex([pd.Timestamp(day)]),
        columns=["open", "high", "low", "close", "volume"],
    )
    c0 = float(day_bar["close"].iloc[0])
    after = [c0 + 1.9 * r, entry + 5.6 * r, entry + 6.1 * r, entry + 5.8 * r, entry + 3.0 * r, entry + 2.6 * r]
    following = _following_days(calendar, day, after, c0)
    daily = pd.concat([hist, day_bar, following])
    daily.index.name = "date"
    desc = (
        f"{symbol}: prior close ₹{prev_close:.2f}; zone ₹{bottom:.2f}–₹{top:.2f} ({zone.describe()}); "
        f"gap-up open ₹{open_px:.2f}; 1-min flush to ₹{dip_low:.2f} at 09:41; 09:42 trades above ₹{flush_high:.2f}; "
        "rally into 3R the same day, 5R the next sessions, then a close below the 9 EMA"
    )
    return Scenario(symbol, day, daily, {day: intraday}, desc, top, bottom)


def breakdown_scenario(settings, calendar: NSECalendar, day: date, symbol: str = "BRKDEMO.SYN", seed: int = 22) -> Scenario:
    """Gap up, then a straight fall through every zone with no rebound bar: no trade, zones broken."""
    hist, zone = _history(settings, calendar, day, symbol, seed)
    top = float(zone.top)
    prev_close = float(hist["close"].iloc[-1])
    open_px = prev_close * 1.01
    floor_px = prev_close * 0.86
    rows: dict[str, list[float]] = {}
    t = datetime.combine(day, time(9, 15))
    n = 375
    for i in range(n):
        a = open_px + (floor_px - open_px) * i / n
        b = open_px + (floor_px - open_px) * (i + 1) / n
        rows[t.strftime("%H:%M")] = _bar_row(a, a, b, b, 6000)  # every bar: lower high, lower low, closes at its low
        t += timedelta(minutes=1)
    intraday = _intraday_frame(day, rows)
    day_bar = pd.DataFrame(
        [[intraday["open"].iloc[0], intraday["high"].max(), intraday["low"].min(), intraday["close"].iloc[-1], intraday["volume"].sum()]],
        index=pd.DatetimeIndex([pd.Timestamp(day)]),
        columns=["open", "high", "low", "close", "volume"],
    )
    daily = pd.concat([hist, day_bar])
    daily.index.name = "date"
    return Scenario(symbol, day, daily, {day: intraday}, f"{symbol}: gap up to ₹{open_px:.2f}, then falls through the ₹{top:.2f} zone and below without a rebound", top, float(zone.bottom))


def write_scenario(store, scenario: Scenario) -> None:
    """Save a scenario into a CsvStore so the CLI / dashboard replay can read it like real data."""
    store.write_daily(scenario.symbol, scenario.daily)
    for d, df in scenario.intraday.items():
        store.write_intraday(scenario.symbol, d, df)
