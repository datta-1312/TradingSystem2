"""Live session monitor (paper by default; live only with every live switch on).

Before the open the pre-market run supplies the watchlist, zones and draft
plans. From the pre-open price discovery (≈09:10 IST) the monitor reads the
expected open, then polls LTP every ``intraday.poll_seconds``, builds 1-minute
bars from the ticks and feeds them, plus every tick, to the same
``SessionEngine`` the replay mode uses.

Positions and paper orders are saved to ``data.state_dir`` so a paper trade
carries over to the next session (GTT stops included).
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Callable

import pandas as pd

from nseswing.core.timeutil import now_ist, to_ist
from nseswing.indicators import atr, ema
from nseswing.intraday.bars import BarBuilder
from nseswing.journal import Journal
from nseswing.levels import ZoneMemory
from nseswing.orders.base import OrderManager
from nseswing.orders.paper import PaperBroker
from nseswing.preopen import expected_open
from nseswing.risk import RiskCalculator
from nseswing.session import SessionEngine, SymbolContext, on_tick
from nseswing.trademanager import TradeManager, load_positions, save_positions

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.alerts import AlertBus
    from nseswing.config import Settings
    from nseswing.core.calendar import NSECalendar
    from nseswing.data.reference import ReferenceData
    from nseswing.pipeline import PreMarketRun


class LiveMonitor:
    def __init__(
        self,
        settings: "Settings",
        calendar: "NSECalendar",
        provider,
        run: "PreMarketRun",
        reference: "ReferenceData",
        alerts: "AlertBus",
        gateway=None,
        journal: Journal | None = None,
        clock: Callable[[], datetime] = now_ist,
        sleep: Callable[[float], None] = _time.sleep,
        day: date | None = None,
        persist: bool = True,
    ):
        self.s = settings
        self.cal = calendar
        self.provider = provider
        self.run_ = run
        self.ref = reference
        self.alerts = alerts
        self.clock = clock
        self.sleep = sleep
        self.day = day or to_ist(clock()).date()
        self.persist = persist
        state = settings.path(settings.data.state_dir)
        self.broker_file = settings.path(settings.execution.paper.state_file)
        self.positions_file = state / "positions.json"
        if gateway is None and settings.execution.mode != "alerts":
            gateway = PaperBroker(settings)
            if persist:
                gateway.load(self.broker_file)
        self.gateway = gateway
        self.journal = journal or Journal(settings.path(settings.journal.db_path))
        self.risk = RiskCalculator(settings)
        self.tm = TradeManager(settings, OrderManager(settings, gateway, alerts), alerts, calendar, self.journal)
        if persist:
            load_positions(self.tm, self.positions_file, self.risk)
        self.memory = ZoneMemory(state / "broken_zones.json") if persist else None

    def _ctx(self, symbol: str, allow_entries: bool) -> SymbolContext | None:
        run = self.run_
        row = run.screen.get(symbol)
        df = run.data.get(symbol)
        lm = run.level_maps.get(symbol)
        if row is None or df is None or lm is None:
            return None
        info = self.ref.get(symbol)
        cas = self.cal.is_cas_stock(self.day, info.is_fno if info else False)
        circuit = self.ref.circuit_limits(symbol, float(df["close"].iloc[-1]), lm.tick, self.s)
        theme = ", ".join(g.split(": ", 1)[-1] for g in row.hot_groups) or (row.industry or "")
        return SymbolContext(
            symbol, row, lm, lm.tick, cas, circuit, info,
            ema9_prev=float(ema(df["close"], self.s.trade_management.extension.ema).iloc[-1]),
            atr_prev=float(atr(df, self.s.trade_management.extension.atr_period).iloc[-1]),
            theme=theme, flags=row.flags, allow_entries=allow_entries,
        )

    def run(self) -> SessionEngine:
        session = self.cal.session(self.day)
        if session is None:
            raise RuntimeError(f"{self.day} is not an NSE session")
        engine = SessionEngine(self.s, self.cal, self.day, self.alerts, self.tm, self.risk, self.memory)
        watch = [c.symbol for c in self.run_.candidates]
        held = [s for s, p in self.tm.positions.items() if p.status == "open"]
        while to_ist(self.clock()) < session.preopen_iep_final_after:
            self.sleep(min(30.0, (to_ist(session.preopen_iep_final_after) - to_ist(self.clock())).total_seconds()))
        quotes = self.provider.quotes(sorted(set(watch) | set(held))) if (watch or held) else {}
        for sym in watch:
            ctx = self._ctx(sym, allow_entries=sym not in held)
            if ctx is None:
                continue
            eo, src = expected_open(sym, ctx.level_map.last_close, self.s, quotes.get(sym))
            engine.add(ctx, eo, src)
        for sym in held:
            if sym not in engine.contexts:
                ctx = self._ctx(sym, allow_entries=False)
                if ctx is not None:
                    engine.add(ctx)
        if isinstance(self.gateway, PaperBroker):
            for sym, ctx in engine.contexts.items():
                self.gateway.set_tick(sym, ctx.tick)
        builders = {sym: BarBuilder(1, session.market_open) for sym in engine.contexts}
        end = to_ist(session.market_close) + timedelta(minutes=1)
        while to_ist(self.clock()) < end and builders:
            now = to_ist(self.clock())
            prices = self.provider.ltp(list(builders))
            for sym, (px, cum_vol) in prices.items():
                for bar in builders[sym].on_tick(now, px, cum_vol):
                    engine.on_minute(sym, bar)
                if session.is_continuous(now, engine.contexts[sym].cas_stock):
                    on_tick(engine, sym, now, px)
            for sym, b in builders.items():
                for bar in b.flush(now):
                    engine.on_minute(sym, bar)
            self._save()
            self.sleep(self.s.intraday.poll_seconds)
        daily_today = {}
        for sym in engine.contexts:
            hist = self.run_.data.get(sym)
            try:
                today = self.provider.daily(sym, self.day - timedelta(days=10), self.day)
            except Exception:  # noqa: BLE001
                today = pd.DataFrame()
            if hist is not None and not today.empty:
                daily_today[sym] = pd.concat([hist[hist.index < pd.Timestamp(self.day)], today[today.index == pd.Timestamp(self.day)]])
        engine.finish(daily_today)
        self._save()
        return engine

    def _save(self) -> None:
        if not self.persist:
            return
        if isinstance(self.gateway, PaperBroker):
            self.gateway.save(self.broker_file)
        save_positions(self.tm, self.positions_file)
