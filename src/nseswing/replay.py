"""Replay mode: step through a historical session bar by bar, in IST, and show
when each rule fires.

    nseswing replay SYMBOL YYYY-MM-DD [--interval 1|5|15] [--days-after N]

What happens:

1. Daily history up to the session before the replay date is loaded and
   adjusted for corporate actions; Stage 1 runs on it (with the universe for
   ranks when available).
2. Stage 2 zones are marked as they stood before the open.
3. Pre-open: the session's actual opening price stands in for the expected
   open (in replay we know it); zones it opened below are marked broken.
4. Every 1-minute bar goes through the same :class:`SessionEngine` the live
   monitor uses: approach, dip, trigger, plan, paper fill, stop on fill, partials,
   extension and circuit checks, the overnight stop at the close.
5. With ``days_after`` the open position keeps being managed on later sessions
   (1-minute bars when available, otherwise daily bars) until it is closed.

Replay always uses the paper broker: it can never send an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pandas as pd

from nseswing.alerts import Alert, AlertBus
from nseswing.core.ticks import TickTable
from nseswing.corpactions import adjust_intraday, adjust_ohlcv
from nseswing.indicators import atr, ema
from nseswing.intraday.bars import Bar, bars_from_frame
from nseswing.journal import Journal
from nseswing.levels import LevelMap, build_level_map
from nseswing.orders.base import OrderManager
from nseswing.orders.paper import PaperBroker
from nseswing.risk import RiskCalculator, TradePlan
from nseswing.screener import Screener, ScreenRow
from nseswing.session import SessionEngine, SymbolContext
from nseswing.trademanager import Position, TradeManager

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings
    from nseswing.core.calendar import NSECalendar
    from nseswing.corpactions import CorporateAction
    from nseswing.data.base import DataProvider
    from nseswing.data.reference import ReferenceData


class ReplayError(RuntimeError):
    pass


@dataclass
class ReplayResult:
    symbol: str
    day: date
    timeframe: int
    row: ScreenRow
    level_map: LevelMap
    intraday: pd.DataFrame  # 1-minute bars of the replay day
    daily: pd.DataFrame  # adjusted daily bars through the last managed session
    alerts: list[Alert]
    plan: TradePlan | None
    draft: TradePlan | None
    position: Position | None
    journal: Journal
    compliance: dict[str, str]
    notes: list[str] = field(default_factory=list)
    trigger_events: list = field(default_factory=list)
    circuit: object | None = None
    history: pd.DataFrame | None = None  # daily bars before the replay day

    def log_lines(self) -> list[str]:
        return [a.line() for a in sorted(self.alerts, key=lambda a: a.ts)]

    def events_until(self, ts: pd.Timestamp) -> list[Alert]:
        return [a for a in self.alerts if pd.Timestamp(a.ts) <= ts]


class ReplayEngine:
    def __init__(
        self,
        settings: "Settings",
        calendar: "NSECalendar",
        provider: "DataProvider",
        reference: "ReferenceData | None" = None,
        actions: "list[CorporateAction] | None" = None,
        universe: dict[str, pd.DataFrame] | None = None,
        manual_anchors: dict[str, list[dict]] | None = None,
    ):
        # replay is always paper, whatever the configured mode
        self.s = settings.model_copy(update={"execution": settings.execution.model_copy(update={"mode": "paper"})})
        self.cal = calendar
        self.provider = provider
        self.ref = reference
        self.actions = actions or []
        self.universe = universe
        self.anchors = manual_anchors or {}
        self.ticks = TickTable.from_settings(self.s)

    def run(self, symbol: str, day: date, interval: int | None = None, days_after: int = 0) -> ReplayResult:
        s = self.s
        if interval is not None:
            s = s.model_copy(update={"intraday": s.intraday.model_copy(update={"timeframe_minutes": interval})})
        session = self.cal.session(day)
        if session is None:
            raise ReplayError(f"{day} is not an NSE session ({self.cal.holiday_name(day) or 'weekend'})")
        notes: list[str] = []

        # 1. daily history, adjusted
        start = day - timedelta(days=int(s.data.daily_history_trading_days * 1.6))
        end = day + timedelta(days=max(10, days_after * 2 + 10))
        raw = self.provider.daily(symbol, start, end)
        if raw.empty:
            raise ReplayError(f"no daily data for {symbol}")
        adj = adjust_ohlcv(raw, [a for a in self.actions if a.symbol == symbol], s.corporate_actions)
        notes += [f"corporate action {a.action.kind} {a.action.ex_date}: {a.decision} {a.note}".strip() for a in adj.actions]
        notes += adj.warnings
        daily = adj.df
        hist = daily[daily.index < pd.Timestamp(day)]
        if len(hist) < s.data.min_history_trading_days:
            raise ReplayError(f"only {len(hist)} daily bars before {day}")

        # 2. Stage 1 as of the previous close
        data = {symbol: hist}
        rank_note = ""
        if self.universe:
            for sym, df in self.universe.items():
                if sym != symbol:
                    data[sym] = df[df.index < pd.Timestamp(day)]
        else:
            rank_note = "no universe data loaded: the top-30% rank condition is judged within this one stock"
            notes.append(rank_note)
        screen = Screener(s, self.ref, self.ticks).run(data)
        row = screen.get(symbol)
        assert row is not None
        lm = build_level_map(symbol, hist, s, row.consolidation, self.anchors.get(symbol))

        # 3. the replay day's 1-minute bars
        intraday = self.provider.intraday(symbol, day, 1)
        if intraday is None or intraday.empty:
            raise ReplayError(f"no 1-minute data for {symbol} on {day}")
        intraday = adjust_intraday(intraday, adj)
        prev_close = float(hist["close"].iloc[-1])
        info = self.ref.get(symbol) if self.ref else None
        tick = self.ref.tick(symbol, prev_close, self.ticks, s) if (self.ref and info) else self.ticks.tick_for(prev_close)
        cas = self.cal.is_cas_stock(day, info.is_fno if info else False)
        circuit = self.ref.circuit_limits(symbol, prev_close, tick, s) if (self.ref and info) else None
        if circuit is None:
            from nseswing.data.reference import ReferenceData

            circuit = ReferenceData({}).circuit_limits(symbol, prev_close, tick, s)
            notes.append(f"price band unknown: circuit limits use the default {s.price_bands.default_band_pct:g}% band")

        alerts = AlertBus(s, console=False, file=None)
        broker = PaperBroker(s)
        broker.set_tick(symbol, tick)
        journal = Journal(":memory:")
        tm = TradeManager(s, OrderManager(s, broker, alerts), alerts, self.cal, journal)
        risk = RiskCalculator(s, self.ticks)
        theme = ", ".join(g.split(": ", 1)[-1] for g in row.hot_groups) or (row.industry or "")
        ctx = SymbolContext(
            symbol, row, lm, tick, cas, circuit, info,
            ema9_prev=float(ema(hist["close"], s.trade_management.extension.ema).iloc[-1]),
            atr_prev=float(atr(hist, s.trade_management.extension.atr_period).iloc[-1]),
            theme=theme, flags=row.flags,
        )
        for c in row.checks():
            alerts.emit("STAGE1", symbol, c.short(), ts=session.preopen_start, check=c.name)
        alerts.emit("STAGE2_ZONES", symbol, " | ".join(z.describe() for z in lm.zones) or "no zones in range", ts=session.preopen_start)

        engine = SessionEngine(s, self.cal, day, alerts, tm, risk)
        first_open = float(intraday["open"].iloc[0])
        engine.add(ctx, expected_open=first_open, source="replay: actual open")
        for b in bars_from_frame(intraday, 1):
            engine.on_minute(symbol, b)
        today = daily[daily.index <= pd.Timestamp(day)]
        if today.index[-1] != pd.Timestamp(day):
            today = pd.concat([today, _day_bar(intraday, day)])
        compliance = engine.finish({symbol: today})

        # 5. keep managing an open position on later sessions
        managed = today
        d = day
        for _ in range(days_after):
            pos = tm.positions.get(symbol)
            if pos is None or pos.status != "open":
                break
            d = self.cal.next_trading_day(d)
            sess = self.cal.session(d)
            upto = daily[daily.index <= pd.Timestamp(d)]
            if upto.empty or upto.index[-1] != pd.Timestamp(d):
                intr = self.provider.intraday(symbol, d, 1)
                if intr is None or intr.empty:
                    notes.append(f"no data for {d}: stopped managing")
                    break
                upto = pd.concat([upto, _day_bar(adjust_intraday(intr, adj), d)])
            prev = upto.iloc[:-1]
            day_ctx = SymbolContext(
                symbol, row, lm, tick, self.cal.is_cas_stock(d, info.is_fno if info else False),
                self.ref.circuit_limits(symbol, float(prev["close"].iloc[-1]), tick, s) if (self.ref and info) else None, info,
                ema9_prev=float(ema(prev["close"], s.trade_management.extension.ema).iloc[-1]),
                atr_prev=float(atr(prev, s.trade_management.extension.atr_period).iloc[-1]),
                theme=theme, flags=row.flags, allow_entries=False,
            )
            eng = SessionEngine(s, self.cal, d, alerts, tm, risk)
            eng.add(day_ctx)
            intr = self.provider.intraday(symbol, d, 1)
            if intr is not None and not intr.empty:
                for b in bars_from_frame(adjust_intraday(intr, adj), 1):
                    eng.on_minute(symbol, b)
            else:
                rowd = upto.iloc[-1]
                bar = Bar(pd.Timestamp(sess.market_open), pd.Timestamp(sess.market_close), float(rowd["open"]), float(rowd["high"]), float(rowd["low"]), float(rowd["close"]), float(rowd["volume"]))
                eng.on_minute(symbol, bar)
                notes.append(f"{d}: managed on the daily bar (no 1-minute data)")
            compliance = eng.finish({symbol: upto})
            managed = upto

        plan = engine.plans.get(symbol)
        return ReplayResult(
            symbol, day, s.intraday.timeframe_minutes, row, lm, intraday, managed, alerts.alerts, plan,
            engine.drafts.get(symbol), tm.positions.get(symbol), journal, compliance, notes,
            engine.trigger_events, circuit, hist,
        )


def _day_bar(intraday: pd.DataFrame, day: date) -> pd.DataFrame:
    return pd.DataFrame(
        [[intraday["open"].iloc[0], intraday["high"].max(), intraday["low"].min(), intraday["close"].iloc[-1], intraday["volume"].sum()]],
        index=pd.DatetimeIndex([pd.Timestamp(day)], name="date"),
        columns=["open", "high", "low", "close", "volume"],
    )
