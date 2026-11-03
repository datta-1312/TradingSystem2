"""Pre-market orchestration: load → adjust → Stage 1 → Stage 2 → pre-open list → draft plans."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pandas as pd

from nseswing.core.ticks import TickTable
from nseswing.corpactions import AdjustmentResult, adjust_ohlcv
from nseswing.levels import LevelMap, ZoneMemory, build_level_map, load_manual_anchors
from nseswing.preopen import PreopenCandidate, preopen_list
from nseswing.risk import RiskCalculator, TradePlan
from nseswing.screener import Screener, ScreenResult

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings
    from nseswing.core.calendar import NSECalendar
    from nseswing.corpactions import CorporateAction
    from nseswing.data.base import DataProvider, Quote
    from nseswing.data.nse import PreopenInfo
    from nseswing.data.reference import ReferenceData

log = logging.getLogger(__name__)


@dataclass
class PreMarketRun:
    as_of: date
    data: dict[str, pd.DataFrame]
    adjustments: dict[str, AdjustmentResult]
    screen: ScreenResult
    level_maps: dict[str, LevelMap]
    candidates: list[PreopenCandidate]
    drafts: dict[str, TradePlan]
    notes: list[str] = field(default_factory=list)


def load_adjusted(
    settings: "Settings",
    provider: "DataProvider",
    symbols: list[str],
    as_of: date,
    actions: "list[CorporateAction]",
) -> tuple[dict[str, pd.DataFrame], dict[str, AdjustmentResult], list[str]]:
    start = as_of - timedelta(days=int(settings.data.daily_history_trading_days * 1.6))
    data, adjustments, notes = {}, {}, []
    by_symbol: dict[str, list] = {}
    for a in actions:
        by_symbol.setdefault(a.symbol, []).append(a)
    for sym in symbols:
        try:
            raw = provider.daily(sym, start, as_of)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the screen
            notes.append(f"{sym}: daily data failed ({exc})")
            continue
        if raw.empty:
            notes.append(f"{sym}: no daily data")
            continue
        adj = adjust_ohlcv(raw, by_symbol.get(sym, []), settings.corporate_actions)
        data[sym], adjustments[sym] = adj.df, adj
        notes.extend(f"{sym}: {w}" for w in adj.warnings)
    return data, adjustments, notes


def run_premarket(
    settings: "Settings",
    calendar: "NSECalendar",
    provider: "DataProvider",
    reference: "ReferenceData",
    actions: "list[CorporateAction] | None" = None,
    as_of: date | None = None,
    quotes: "dict[str, Quote] | None" = None,
    preopen: "dict[str, PreopenInfo] | None" = None,
    symbols: list[str] | None = None,
    data: dict[str, pd.DataFrame] | None = None,
) -> PreMarketRun:
    as_of = as_of or calendar.previous_trading_day(date.today() + timedelta(days=1))
    notes: list[str] = []
    adjustments: dict[str, AdjustmentResult] = {}
    if data is None:
        data, adjustments, notes = load_adjusted(settings, provider, symbols or reference.symbols(), as_of, actions or [])
    data = {s: df[df.index <= pd.Timestamp(as_of)] for s, df in data.items()}
    ticks = TickTable.from_settings(settings)
    screen = Screener(settings, reference, ticks).run(data, quotes)
    anchors = load_manual_anchors(settings.path(settings.levels.avwap.manual_anchors_file))
    memory = ZoneMemory(settings.path(settings.data.state_dir) / "broken_zones.json")
    maps: dict[str, LevelMap] = {}
    for row in screen.watchlist:
        if not row.all_checks_pass:
            continue
        df = data[row.symbol]
        tick = reference.tick(row.symbol, float(df["close"].iloc[-1]), ticks, settings)
        maps[row.symbol] = build_level_map(row.symbol, df, settings, row.consolidation, anchors.get(row.symbol), memory, tick)
    candidates = preopen_list(screen, maps, settings, quotes, preopen)
    risk = RiskCalculator(settings, ticks)
    drafts: dict[str, TradePlan] = {}
    for c in candidates:
        if c.zone is None:
            continue
        row = screen.get(c.symbol)
        lm = maps[c.symbol]
        circuit = reference.circuit_limits(c.symbol, lm.last_close, lm.tick, settings, (quotes or {}).get(c.symbol))
        theme = ", ".join(g.split(": ", 1)[-1] for g in row.hot_groups) or (row.industry or "")
        try:
            drafts[c.symbol] = risk.draft_plan(c.symbol, c.zone, lm.tick, theme=theme, flags=row.flags, circuit=circuit)
        except ValueError as exc:
            notes.append(f"{c.symbol}: no draft plan ({exc})")
    return PreMarketRun(as_of, data, adjustments, screen, maps, candidates, drafts, notes)
