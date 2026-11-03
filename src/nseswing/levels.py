"""Stage 2: mark support zones on the daily chart.

1. Levels: 9/21/50 EMAs, prior swing highs (resistance that price has broken,
   now support), swing lows, unfilled gap-ups, and anchored VWAPs.
2. AVWAP: default anchor = the bar that made the consolidation high; manual
   anchors from ``config/anchors.yaml``. Tracks reclaim and pullback toward it.
3. Confluence: levels within ``confluence.tolerance_pct`` form one zone. A zone
   holding the AVWAP, the 21 EMA and a prior high is HIGH priority; two or more
   different level types is MEDIUM; a single level is LOW.
4. First retest: a zone containing a level price broke above and has not come
   back to since is flagged as the priority setup.
8. Broken zones: zones the intraday monitor saw price fall straight through are
   remembered (``ZoneMemory``) and shown as broken until price closes back above.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd
import yaml

from nseswing.core.ticks import TickTable, round_to_tick
from nseswing.indicators import Gap, Swing, avwap, avwap_state, ema, find_gaps, swing_points

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings
    from nseswing.screener import Consolidation

Priority = Literal["high", "medium", "low"]
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Level:
    kind: str  # ema9 | ema21 | ema50 | avwap | prior_high | swing_low | gap
    price: float
    label: str
    date: pd.Timestamp | None = None
    low: float | None = None  # range levels (gaps)
    high: float | None = None
    first_retest: str | None = None  # None | "pending" | "in_progress"
    break_date: pd.Timestamp | None = None

    @property
    def type(self) -> str:
        return "ma" if self.kind.startswith("ema") else self.kind


@dataclass
class Zone:
    symbol: str
    bottom: Decimal
    top: Decimal
    levels: list[Level]
    priority: Priority
    broken: bool = False
    broken_on: date | None = None

    @property
    def zone_id(self) -> str:
        return f"{self.symbol}:{self.bottom}-{self.top}"

    @property
    def kinds(self) -> set[str]:
        return {lv.kind for lv in self.levels}

    @property
    def first_retest(self) -> bool:
        return any(lv.first_retest for lv in self.levels)

    @property
    def mid(self) -> float:
        return float(self.bottom + self.top) / 2

    def contains(self, price: float) -> bool:
        return float(self.bottom) <= price <= float(self.top)

    def describe(self) -> str:
        parts = " + ".join(lv.label for lv in sorted(self.levels, key=lambda lv: -lv.price))
        tag = " [FIRST RETEST]" if self.first_retest else ""
        broken = f" [BROKEN {self.broken_on}]" if self.broken else ""
        return f"{self.priority.upper()} ₹{self.bottom}–₹{self.top}: {parts}{tag}{broken}"


@dataclass
class AvwapLine:
    label: str
    anchor_date: pd.Timestamp
    anchor_pos: int
    source: Literal["default", "manual"]
    series: pd.Series
    state: dict = field(default_factory=dict)


@dataclass
class LevelMap:
    symbol: str
    as_of: pd.Timestamp
    last_close: float
    tick: Decimal
    emas: dict[int, pd.Series]
    avwaps: list[AvwapLine]
    swing_highs: list[Swing]
    swing_lows: list[Swing]
    gaps: list[Gap]
    levels: list[Level]
    zones: list[Zone]  # highest first

    def support_zones(self, include_broken: bool = False) -> list[Zone]:
        return [z for z in self.zones if include_broken or not z.broken]

    def zones_below(self, price: float, include_broken: bool = False) -> list[Zone]:
        """Zones at or below ``price`` (top first): the next supports on a pullback."""
        return [z for z in self.support_zones(include_broken) if float(z.bottom) <= price]

    @property
    def default_avwap(self) -> AvwapLine | None:
        return next((a for a in self.avwaps if a.source == "default"), None)


# ------------------------------------------------------------------ broken-zone memory (Stage 2.8)
class ZoneMemory:
    """Persists zones the intraday monitor marked broken."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.records: list[dict] = []
        if path and path.exists():
            self.records = json.loads(path.read_text(encoding="utf-8"))

    def mark_broken(self, symbol: str, bottom: Decimal, top: Decimal, on: date, reason: str) -> None:
        self.records.append({"symbol": symbol, "bottom": str(bottom), "top": str(top), "broken_on": on.isoformat(), "reason": reason})
        self.save()

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.records, indent=1), encoding="utf-8")

    def apply(self, zone: Zone, daily: pd.DataFrame, memory_days: int) -> None:
        """Mark ``zone`` broken if a recent record overlaps it and price has not closed back above."""
        keep = []
        for rec in self.records:
            if rec["symbol"] != zone.symbol:
                keep.append(rec)
                continue
            broken_on = date.fromisoformat(rec["broken_on"])
            after = daily[daily.index > pd.Timestamp(broken_on)]
            reclaimed = bool((after["close"] > float(rec["top"])).any())
            too_old = len(after) > memory_days
            if reclaimed or too_old:
                continue  # forget it
            keep.append(rec)
            if Decimal(rec["bottom"]) <= zone.top and zone.bottom <= Decimal(rec["top"]):
                zone.broken, zone.broken_on = True, broken_on
        self.records = keep


# ------------------------------------------------------------------ anchors
def load_manual_anchors(path: Path) -> dict[str, list[dict]]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): list(v or []) for k, v in (raw.get("anchors") or {}).items()}


def _pos_on_or_after(df: pd.DataFrame, when) -> int | None:
    ts = pd.Timestamp(when)
    hits = (df.index >= ts).nonzero()[0]
    return int(hits[0]) if len(hits) else None


# ------------------------------------------------------------------ first retest (Stage 2.4)
def retest_status(level: float | pd.Series, df: pd.DataFrame, start_pos: int, touch_pct: float) -> tuple[str | None, pd.Timestamp | None]:
    """Status of the first retest after price last broke above ``level`` (static or a series).

    Returns ("pending", break_date) when price broke above and has not come back,
    ("in_progress", break_date) when the latest bar is the first touch since the
    break, or (None, break_date) when it was already retested or the level was lost.
    """
    n = len(df)
    lvl = level if isinstance(level, pd.Series) else pd.Series(float(level), index=df.index)
    close, low = df["close"], df["low"]
    above = close > lvl
    crossed = above & ~above.shift(1, fill_value=True)
    crosses = [i for i in range(max(start_pos + 1, 1), n) if bool(crossed.iloc[i])]
    if not crosses:
        return None, None
    b = crosses[-1]
    if not bool(above.iloc[-1]):
        return None, df.index[b]
    touches = [j for j in range(b + 1, n) if low.iloc[j] <= lvl.iloc[j] * (1 + touch_pct / 100.0)]
    if not touches:
        return "pending", df.index[b]
    if touches == [n - 1]:
        return "in_progress", df.index[b]
    return None, df.index[b]


# ------------------------------------------------------------------ main builder
def build_level_map(
    symbol: str,
    df: pd.DataFrame,
    settings: "Settings",
    consolidation: "Consolidation | None" = None,
    manual_anchors: list[dict] | None = None,
    memory: ZoneMemory | None = None,
    tick: Decimal | None = None,
) -> LevelMap:
    lv = settings.levels
    cf = lv.confluence
    ticks = TickTable.from_settings(settings)
    close = df["close"]
    last = float(close.iloc[-1])
    tick = tick or ticks.tick_for(last)
    lo_bound, hi_bound = last * (1 - cf.search_below_pct / 100), last * (1 + cf.search_above_pct / 100)
    start = max(0, len(df) - lv.lookback_days)
    in_range = lambda p: lo_bound <= p <= hi_bound  # noqa: E731
    levels: list[Level] = []

    # EMAs
    emas = {p: ema(close, p) for p in lv.emas}
    for p, series in emas.items():
        v = float(series.iloc[-1])
        if in_range(v):
            levels.append(Level(f"ema{p}", v, f"EMA{p} ₹{v:.2f}", df.index[-1]))

    # AVWAPs
    if consolidation is None:
        from nseswing.screener import find_consolidation

        consolidation = find_consolidation(df, settings)
    lines: list[AvwapLine] = []
    lines.append(_avwap_line(df, consolidation.high_pos, f"AVWAP from {consolidation.high_date:%d-%b} high", "default", settings))
    for a in manual_anchors or []:
        pos = _pos_on_or_after(df, a["date"])
        if pos is not None:
            lines.append(_avwap_line(df, pos, f"AVWAP {a.get('label') or ''} ({pd.Timestamp(a['date']):%d-%b})".replace("  ", " "), "manual", settings))
    for line in lines:
        v = float(line.series.iloc[-1])
        status, brk = retest_status(line.series, df, line.anchor_pos, lv.retest.touch_tolerance_pct)
        if in_range(v):
            levels.append(Level("avwap", v, f"{line.label} ₹{v:.2f}", line.anchor_date, first_retest=status, break_date=brk))

    # swing highs that price has broken (prior highs → support) and swing lows
    highs = swing_points(df, lv.swing_lookaround, "high")
    lows = swing_points(df, lv.swing_lookaround, "low")
    important = {s.pos for s in swing_points(df, lv.retest.important_swing_lookaround, "high")}
    for s in highs:
        if s.pos < start or not in_range(s.price) or s.price > last:
            continue
        broken_above = bool((close.iloc[s.pos + 1 :] > s.price).any())
        if not broken_above:
            continue
        status, brk = (None, None)
        if s.pos in important:
            status, brk = retest_status(s.price, df, s.pos, lv.retest.touch_tolerance_pct)
        tag = "important prior high" if s.pos in important else "prior high"
        levels.append(Level("prior_high", s.price, f"{tag} ₹{s.price:.2f} ({s.date:%d-%b})", s.date, first_retest=status, break_date=brk))
    for s in lows:
        if s.pos >= start and in_range(s.price) and s.price <= last:
            levels.append(Level("swing_low", s.price, f"swing low ₹{s.price:.2f} ({s.date:%d-%b})", s.date))

    # unfilled gap-ups below price
    gaps = find_gaps(df, lv.gaps.min_gap_pct, lv.gaps.lookback_days)
    for g in gaps:
        if g.direction == "up" and not g.filled and in_range(g.top) and g.top <= last:
            levels.append(Level("gap", g.top, f"unfilled gap ₹{g.bottom:.2f}–₹{g.top:.2f} ({g.date:%d-%b})", g.date, low=g.bottom, high=g.top))

    zones = _cluster(symbol, levels, settings, tick)
    if memory is not None:
        for z in zones:
            memory.apply(z, df, lv.broken_zone_memory_days)
        memory.save()
    return LevelMap(symbol, df.index[-1], last, tick, emas, lines, highs, lows, gaps, levels, zones)


def _avwap_line(df: pd.DataFrame, pos: int, label: str, source: str, settings: "Settings") -> AvwapLine:
    series = avwap(df, pos, settings.levels.avwap.price)
    state = avwap_state(df["close"], df["low"], series, settings.levels.avwap.pullback_near_pct)
    return AvwapLine(label, df.index[pos], pos, source, series, state)  # type: ignore[arg-type]


def _cluster(symbol: str, levels: list[Level], settings: "Settings", tick: Decimal) -> list[Zone]:
    cf = settings.levels.confluence
    mode = settings.ticks.rounding.zone
    ordered = sorted(levels, key=lambda lv: -lv.price)
    clusters: list[list[Level]] = []
    for level in ordered:
        if clusters and (clusters[-1][0].price - level.price) / level.price * 100 <= cf.tolerance_pct:
            clusters[-1].append(level)
        else:
            clusters.append([level])
    zones = []
    required = set(cf.high_priority_requires)
    for members in clusters:
        kinds = {m.kind for m in members}
        types = {m.type for m in members}
        if required <= kinds:
            priority: Priority = "high"
        elif len(types) >= cf.medium_priority_min_types:
            priority = "medium"
        else:
            priority = "low"
        lo = min(m.price for m in members) * (1 - cf.zone_pad_pct / 100)
        hi = max(m.price for m in members) * (1 + cf.zone_pad_pct / 100)
        zones.append(Zone(symbol, round_to_tick(lo, tick, mode), round_to_tick(hi, tick, mode), members, priority))
    return zones


def best_zone(zones: list[Zone]) -> Zone | None:
    """Highest-priority unbroken zone, first retests first, nearest (highest) first."""
    live = [z for z in zones if not z.broken]
    if not live:
        return None
    return sorted(live, key=lambda z: (_PRIORITY_ORDER[z.priority], not z.first_retest, -float(z.top)))[0]


def zones_to_records(zones: list[Zone]) -> list[dict]:
    out = []
    for z in zones:
        out.append(
            {
                "zone_id": z.zone_id,
                "bottom": float(z.bottom),
                "top": float(z.top),
                "priority": z.priority,
                "first_retest": z.first_retest,
                "broken": z.broken,
                "levels": [{k: (str(v) if isinstance(v, (pd.Timestamp, Decimal)) else v) for k, v in asdict(lv).items()} for lv in z.levels],
            }
        )
    return out
