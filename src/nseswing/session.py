"""One trading session for a set of watchlist stocks.

The same engine runs the replay mode and the live/paper monitor:

    pre-open  → add(): expected open vs zones, zones broken at the open, draft plan
    each 1-min bar → on_minute():
        1. an open position: paper/live stop and target fills, Stage 4 checks
        2. no entry yet: build the monitored 1/5/15-min bar, run the trigger engine
           (intrabar on each minute, state updates when the bar completes)
        3. on a valid trigger: Stage 3 plan → entry → stop placed on the fill
    continuous trading ends (15:15 CAS / 15:30) → overnight stop handling
    finish() after the close → 9-EMA trail check, extension, overnight compliance
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd

from nseswing.core.timeutil import to_ist
from nseswing.intraday.bars import Bar
from nseswing.intraday.trigger import State, TriggerEngine, TriggerEvent
from nseswing.preopen import classify

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.alerts import AlertBus
    from nseswing.config import Settings
    from nseswing.core.calendar import NSECalendar
    from nseswing.data.reference import CircuitLimits, SecurityInfo
    from nseswing.levels import LevelMap, ZoneMemory
    from nseswing.risk import RiskCalculator, TradePlan
    from nseswing.screener import ScreenRow
    from nseswing.trademanager import TradeManager

_ALERT_KIND = {
    "approach": ("ZONE_APPROACH", "info"),
    "dip": ("DIP_INTO_ZONE", "info"),
    "trigger": ("TRIGGER", "warning"),
    "invalid_trigger": ("TRIGGER_INVALID", "warning"),
    "zone_broken": ("ZONE_BROKEN", "warning"),
    "next_zone": ("NEXT_ZONE", "info"),
    "no_zones": ("NO_ZONES", "info"),
    "session_end": ("SESSION_END", "info"),
}


@dataclass
class SymbolContext:
    symbol: str
    row: "ScreenRow"
    level_map: "LevelMap"
    tick: Decimal
    cas_stock: bool = False
    circuit: "CircuitLimits | None" = None
    info: "SecurityInfo | None" = None
    ema9_prev: float | None = None
    atr_prev: float | None = None
    theme: str = ""
    flags: list[str] = field(default_factory=list)
    allow_entries: bool = True


class SessionEngine:
    def __init__(
        self,
        settings: "Settings",
        calendar: "NSECalendar",
        day: date,
        alerts: "AlertBus",
        trade_manager: "TradeManager",
        risk: "RiskCalculator",
        memory: "ZoneMemory | None" = None,
    ):
        self.s = settings
        self.cal = calendar
        self.day = day
        self.session = calendar.session(day)
        if self.session is None:
            raise ValueError(f"{day} is not an NSE trading session")
        self.alerts = alerts
        self.tm = trade_manager
        self.risk = risk
        self.memory = memory
        self.tf = settings.intraday.timeframe_minutes
        self.contexts: dict[str, SymbolContext] = {}
        self.engines: dict[str, TriggerEngine] = {}
        self.forming: dict[str, Bar | None] = {}
        self.plans: dict[str, "TradePlan"] = {}
        self.drafts: dict[str, "TradePlan"] = {}
        self.trigger_events: list[TriggerEvent] = []
        self._closed: set[str] = set()
        self._started: set[str] = set()

    # ------------------------------------------------------------------ pre-open (Stage 2.5)
    def add(self, ctx: SymbolContext, expected_open: float | None = None, source: str = "") -> None:
        self.contexts[ctx.symbol] = ctx
        self.forming[ctx.symbol] = None
        if not ctx.allow_entries:
            return
        ts = self.session.preopen_iep_final_after
        zones = ctx.level_map.support_zones()
        if expected_open is not None:
            zone, dist, status, broken = classify(ctx.level_map, expected_open, self.s)
            gap = (expected_open / ctx.level_map.last_close - 1) * 100
            for z in broken:
                z.broken, z.broken_on = True, self.day
                if self.memory is not None:
                    self.memory.mark_broken(ctx.symbol, z.bottom, z.top, self.day, "opened below the zone")
                self.alerts.emit("ZONE_BROKEN", ctx.symbol, f"expected open ₹{expected_open:.2f} is below zone ₹{z.bottom}–₹{z.top}: broken at the open", level="warning", ts=ts)
            if zone is not None:
                where = {"inside": "inside", "near": f"{dist:.2f}% above", "far": f"{dist:.2f}% above"}[status]
                self.alerts.emit(
                    "PREOPEN_WATCH" if status in ("inside", "near") else "PREOPEN_FAR",
                    ctx.symbol,
                    f"expected open ₹{expected_open:.2f} ({gap:+.2f}% gap, {source}) is {where} zone {zone.describe()}",
                    ts=ts,
                )
                if ctx.row.all_checks_pass:
                    try:
                        self.drafts[ctx.symbol] = self.risk.draft_plan(ctx.symbol, zone, ctx.tick, theme=ctx.theme, flags=ctx.flags, circuit=ctx.circuit)
                    except ValueError:
                        pass
            zones = [z for z in zones if not z.broken]
        self.engines[ctx.symbol] = TriggerEngine(
            ctx.symbol, zones, ctx.tick, self.s, self.session, ctx.row.all_checks_pass, ctx.cas_stock, ctx.circuit, self.memory
        )
        if not ctx.row.all_checks_pass:
            self.alerts.emit("STAGE1_FAIL", ctx.symbol, "does not pass every Stage 1 check: any trigger today is invalid", level="warning", ts=ts)

    # ------------------------------------------------------------------ bars
    def on_minute(self, symbol: str, bar: Bar) -> None:
        ctx = self.contexts[symbol]
        cont = self.session.is_continuous(bar.start, ctx.cas_stock)
        if symbol not in self._started and cont:
            self._started.add(symbol)
            self.tm.start_of_session(symbol, self.session, bar)
        lower = ctx.circuit.lower if ctx.circuit else None
        upper = ctx.circuit.upper if ctx.circuit else None
        pos = self.tm.positions.get(symbol)
        if pos is not None and pos.status == "open" and pos.entry_time < bar.start:
            fills = self.tm.broker.on_bar(symbol, bar, cont, lower) if hasattr(self.tm.broker, "on_bar") else []
            self._broker_notes(symbol)
            self.tm.on_fills(fills)
            if cont:
                self.tm.on_bar(symbol, bar, ctx.ema9_prev, ctx.atr_prev, upper, lower)
        eng = self.engines.get(symbol)
        if eng is not None and eng.state not in (State.DONE, State.TRIGGERED):
            events: list[TriggerEvent] = []
            if self.tf == 1:
                events += eng.on_bar(bar)
            else:
                forming = self._aggregate(symbol, bar)
                if self.s.intraday.evaluation == "intrabar":
                    events += eng.check_intrabar(forming, bar)
                if bar.end >= forming.end:
                    events += eng.on_bar(forming)
                    self.forming[symbol] = None
            self._handle(ctx, eng, events)
        elif eng is not None and self.tf > 1:
            forming = self._aggregate(symbol, bar)
            if bar.end >= forming.end:
                self.forming[symbol] = None
        if not cont and symbol not in self._closed and bar.start >= self.session.continuous_end(ctx.cas_stock):
            self.end_continuous(symbol, bar.start)

    def _aggregate(self, symbol: str, bar: Bar) -> Bar:
        cur = self.forming.get(symbol)
        if cur is None or bar.start >= cur.end:
            open_ = self.session.market_open
            n = int((to_ist(bar.start) - to_ist(open_)) / pd.Timedelta(minutes=self.tf))
            start = to_ist(open_) + pd.Timedelta(minutes=self.tf * n)
            cur = Bar(start, start + pd.Timedelta(minutes=self.tf), bar.open, bar.high, bar.low, bar.close, bar.volume)
        else:
            cur = replace(cur, high=max(cur.high, bar.high), low=min(cur.low, bar.low), close=bar.close, volume=cur.volume + bar.volume)
        self.forming[symbol] = cur
        return cur

    def _broker_notes(self, symbol: str) -> None:
        notes = getattr(self.tm.broker, "notes", None)
        if not notes:
            return
        keep = []
        for ts, sym, msg in notes:
            if sym == symbol:
                self.alerts.emit("BROKER_NOTE", sym, msg, level="warning", ts=ts)
            else:
                keep.append((ts, sym, msg))
        notes[:] = keep

    def _handle(self, ctx: SymbolContext, eng: TriggerEngine, events: list[TriggerEvent]) -> None:
        for ev in events:
            self.trigger_events.append(ev)
            kind, level = _ALERT_KIND.get(ev.kind, (ev.kind.upper(), "info"))
            self.alerts.emit(kind, ev.symbol, ev.message, level=level, ts=ev.ts)
        sig = eng.signal
        if sig is None or ctx.symbol in self.plans or not ctx.allow_entries:
            return
        plan = self.risk.plan_from_signal(sig, ctx.tick, theme=ctx.theme, flags=ctx.flags, circuit=ctx.circuit)
        self.plans[ctx.symbol] = plan
        self.alerts.emit(
            "PLAN", ctx.symbol,
            f"entry ≈₹{plan.entry}, stop ₹{plan.stop} ({plan.stop_basis}), {plan.shares} shares, "
            f"₹ risk {plan.planned_loss}, {plan.position_pct}% of account, stop {plan.stop_pct}%, 3R ₹{plan.targets.get('3R')}, 5R ₹{plan.targets.get('5R')}",
            ts=sig.ts,
        )
        for w in plan.warnings[:-1]:  # the last one is the standing gap/slippage warning
            self.alerts.emit("PLAN_WARNING", ctx.symbol, w, level="warning", ts=sig.ts)
        if plan.shares > 0:
            self.tm.enter(plan, sig.ts, sig.fill_reference, ctx.cas_stock)

    # ------------------------------------------------------------------ session end
    def end_continuous(self, symbol: str, ts) -> None:
        if symbol in self._closed:
            return
        self._closed.add(symbol)
        eng = self.engines.get(symbol)
        if eng is not None and eng.state not in (State.DONE, State.TRIGGERED):
            eng.state = State.DONE
        self.tm.end_of_continuous_session(symbol, ts)

    def finish(self, daily_today: dict[str, pd.DataFrame]) -> dict[str, str]:
        """After the close: end any stock still open, trail/extension checks, overnight compliance."""
        for sym, ctx in self.contexts.items():
            self.end_continuous(sym, self.session.continuous_end(ctx.cas_stock))
        close_ts = self.session.market_close
        for sym in self.contexts:
            df = daily_today.get(sym)
            if df is not None:
                self.tm.end_of_day(sym, df, close_ts)
        return self.tm.overnight_compliance(close_ts + pd.Timedelta(minutes=1))


def on_tick(engine: SessionEngine, symbol: str, ts, price: float) -> None:
    """Live ticks: fire a trigger the moment price trades above the previous bar's high."""
    eng = engine.engines.get(symbol)
    ctx = engine.contexts.get(symbol)
    if eng is None or ctx is None or eng.state in (State.DONE, State.TRIGGERED):
        return
    engine._handle(ctx, eng, eng.on_price(ts, price))
