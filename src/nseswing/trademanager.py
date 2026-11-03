"""Stage 4: take partial profits, then manage the rest.

* R = initial risk per share (entry fill − initial stop). Targets at 1R/3R/5R.
* Partials: ``trade_management.partials`` (fraction of the original quantity)
  near 3R and/or 5R; skipped when the position's hold-full override is on.
* Trailing: once the first target is reached (default), the rest is sold when
  a daily close is below the 9 EMA (at the next open by default).
* Extension: close stretched ≥ X% or ≥ N ATR above the 9 EMA → "trim into strength".
* Circuits: near/at the upper circuit → trim hint; at the lower circuit → the stop
  cannot fill while the stock is locked.
* Overnight: when continuous trading ends, a DAY stop expires (CAS stocks: the
  exchange cancels it at 15:15). The position then gets a GTT stop, or the flag
  ``REPLACE_SL_NEXT_MORNING`` and the SL is re-placed at 09:15.
* Manual override: ``set_hold_full`` keeps the full position; ``hold_full_hint``
  shows whether the market is strong and the other positions have cushions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING

import pandas as pd

from nseswing.core.ticks import round_to_tick, to_decimal
from nseswing.core.timeutil import to_ist
from nseswing.costs import ChargeModel, LegCharges
from nseswing.indicators import atr, ema, slope_pct_per_day
from nseswing.orders.base import Fill, Order, OrderManager, Side, Status

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.alerts import AlertBus
    from nseswing.config import Settings
    from nseswing.core.calendar import NSECalendar, Session
    from nseswing.intraday.bars import Bar
    from nseswing.journal import Journal
    from nseswing.risk import TradePlan

REPLACE_FLAG = "REPLACE_SL_NEXT_MORNING"


@dataclass
class Position:
    symbol: str
    plan: "TradePlan"
    trade_id: int | None
    entry_time: pd.Timestamp
    entry_price: Decimal
    qty_initial: int
    qty_open: int
    initial_stop: Decimal
    stop: Decimal
    tick: Decimal
    cas_stock: bool
    settle_date: date
    targets: dict[str, Decimal] = field(default_factory=dict)
    stop_order: Order | None = None
    stop_kind: str | None = None  # DAY_SL | GTT | None
    partials_done: set[str] = field(default_factory=set)
    alerted: set[str] = field(default_factory=set)
    hold_full: bool = False
    first_target_reached: bool = False
    pending_exit: str | None = None
    flags: set[str] = field(default_factory=set)
    status: str = "open"
    fills: list[Fill] = field(default_factory=list)

    @property
    def r_per_share(self) -> Decimal:
        return self.entry_price - self.initial_stop

    @property
    def trail_active(self) -> bool:
        return self.first_target_reached

    def unrealized_r(self, price: float | Decimal) -> Decimal:
        return ((to_decimal(price) - self.entry_price) / self.r_per_share).quantize(Decimal("0.01")) if self.r_per_share else Decimal(0)


class TradeManager:
    def __init__(
        self,
        settings: "Settings",
        orders: OrderManager,
        alerts: "AlertBus",
        calendar: "NSECalendar",
        journal: "Journal | None" = None,
        costs: ChargeModel | None = None,
    ):
        self.s = settings
        self.tm = settings.trade_management
        self.orders = orders
        self.alerts = alerts
        self.cal = calendar
        self.journal = journal
        self.costs = costs or ChargeModel(settings)
        self.positions: dict[str, Position] = {}

    @property
    def broker(self):
        return self.orders.gw

    # ------------------------------------------------------------------ entry (Stage 3.5)
    def enter(self, plan: "TradePlan", ts: datetime, reference_price: Decimal, cas_stock: bool) -> Position | None:
        ticket = self.orders.submit_entry(plan, ts, reference_price)
        if ticket.alert_only or ticket.fill is None:
            return None
        fill = ticket.fill
        entry = fill.price
        pos = Position(
            plan.symbol, plan, None, fill.ts, entry, fill.qty, fill.qty, plan.stop, plan.stop, plan.tick, cas_stock,
            self.cal.settlement_date(fill.ts.date(), self.s.settlement.cycle_days),
        )
        rps = pos.r_per_share
        for k in self.s.targets.r_multiples:
            pos.targets[f"{k.normalize()}R"] = round_to_tick(entry + rps * k, plan.tick, self.s.ticks.rounding.target)
        pos.stop_order, pos.stop_kind = ticket.stop, ("DAY_SL" if ticket.stop else None)
        pos.fills.append(fill)
        if self.journal is not None:
            pos.trade_id = self.journal.open_trade(plan, self.s.execution.mode, fill.ts.to_pydatetime(), entry, fill.qty, plan.stop)
            self.journal.add_fill(pos.trade_id, fill.ts.to_pydatetime(), "BUY", fill.qty, entry, "entry", self.costs.leg("BUY", fill.qty, entry, plan.product))
        if entry != plan.entry:
            self.alerts.emit("R_RECOMPUTED", plan.symbol, f"filled ₹{entry} vs planned ₹{plan.entry}: R = ₹{rps}, targets {', '.join(f'{k} ₹{v}' for k, v in pos.targets.items())}", ts=fill.ts)
        self.positions[plan.symbol] = pos
        return pos

    # ------------------------------------------------------------------ fills from the broker
    def on_fills(self, fills: list[Fill]) -> None:
        for f in fills:
            pos = self.positions.get(f.symbol)
            if pos is None or pos.status != "open" or f.side != Side.SELL:
                continue
            self._record_sell(pos, f)

    def _record_sell(self, pos: Position, f: Fill) -> None:
        pos.qty_open -= f.qty
        pos.fills.append(f)
        day = f.ts.date()
        same_day = day == pos.entry_time.date()
        btst = (not same_day) and day <= pos.settle_date
        product = "MIS" if same_day else pos.plan.product
        dp = False if same_day else (self.s.costs.dp_on_btst_sell if btst else True)
        charges = self.costs.leg("SELL", f.qty, f.price, product, dp_applies=dp)
        r = ((f.price - pos.entry_price) / pos.r_per_share).quantize(Decimal("0.01"))
        label = {"stop": "STOP_HIT", "target": "TARGET_PARTIAL", "exit": "EXIT"}.get(f.purpose, "EXIT")
        self.alerts.emit(label, pos.symbol, f"sold {f.qty} @ ₹{f.price} ({r:+}R gross), {pos.qty_open} left" + (" [same-day: taxed as intraday]" if same_day else ""), ts=f.ts, level="warning" if f.purpose == "stop" else "info")
        if self.journal is not None and pos.trade_id is not None:
            self.journal.add_fill(pos.trade_id, f.ts.to_pydatetime(), "SELL", f.qty, f.price, f.purpose, charges)
            if same_day:  # the bought shares sold today were taxed as delivery at entry: correct to intraday rates
                adj_qty = f.qty
                delta = self.costs.leg("BUY", adj_qty, pos.entry_price, "MIS").total - self.costs.leg("BUY", adj_qty, pos.entry_price, "CNC").total
                adj = LegCharges("BUY", 0, pos.entry_price, Decimal(0), stt=delta)
                self.journal.add_fill(pos.trade_id, f.ts.to_pydatetime(), "ADJ", 0, Decimal(0), "intraday-tax-adjustment", adj)
        if pos.qty_open <= 0:
            self._close(pos, f.ts)
        elif pos.stop_order is not None and pos.stop_order.is_open:
            self.broker.modify_qty(pos.stop_order.order_id, pos.qty_open)

    def _close(self, pos: Position, ts: pd.Timestamp) -> None:
        pos.status = "closed"
        for o in self.broker.open_orders(pos.symbol):
            self.broker.cancel(o.order_id, ts)
        msg = "position closed"
        if self.journal is not None and pos.trade_id is not None:
            res = self.journal.close_trade(pos.trade_id, ts.to_pydatetime())
            msg += f": {res.r_gross:+}R gross, {res.r_net:+}R net (charges ₹{res.charges})"
        self.alerts.emit("POSITION_CLOSED", pos.symbol, msg, ts=ts)

    # ------------------------------------------------------------------ intraday management
    def on_bar(self, symbol: str, bar: "Bar", ema9_ref: float | None = None, atr_ref: float | None = None, upper: Decimal | None = None, lower: Decimal | None = None) -> None:
        pos = self.positions.get(symbol)
        if pos is None or pos.status != "open":
            return
        day = bar.start.date().isoformat()
        if pos.stop_order is not None and pos.stop_order.status == Status.TRIGGERED and "gtt_unfilled" not in pos.alerted:
            pos.alerted.add("gtt_unfilled")
            pos.flags.add("STOP_UNFILLED")
            self.alerts.emit("STOP_UNFILLED", symbol, f"stop ₹{pos.stop} triggered but its limit order is UNFILLED (price ₹{bar.close:.2f}): exit manually", level="critical", ts=bar.start)
        high, close = to_decimal(bar.high), to_decimal(bar.close)
        for p in sorted(self.tm.partials, key=lambda x: x.at_r):
            label = f"{p.at_r.normalize()}R"
            target = pos.targets.get(label) or round_to_tick(pos.entry_price + pos.r_per_share * p.at_r, pos.tick, "down")
            if high >= target and not pos.first_target_reached:
                pos.first_target_reached = True
                if self.tm.trail.activation == "first_target":
                    self.alerts.emit("TRAIL_ACTIVE", symbol, f"{label} ₹{target} reached: trailing the rest with the {self.tm.trail.ema}-day EMA", ts=bar.start)
            if label in pos.partials_done or pos.status != "open":
                continue
            if pos.hold_full:
                if high >= target and f"hold_{label}" not in pos.alerted:
                    pos.alerted.add(f"hold_{label}")
                    self.alerts.emit("HOLD_FULL", symbol, f"{label} ₹{target} reached; hold-full override ON, so no partial sold", ts=bar.start)
                continue
            if high >= target:
                qty = self._partial_qty(pos, p.fraction)
                if qty <= 0:
                    continue
                pos.partials_done.add(label)
                fills = self.orders.exit(symbol, qty, bar.start, f"partial at {label}", limit=target, reference_price=max(target, to_decimal(bar.open)), purpose="target")
                self.on_fills(fills)
                if self.tm.move_stop_to_breakeven_after_first_partial and pos.status == "open" and pos.stop < pos.entry_price:
                    self._move_stop(pos, pos.entry_price, bar.start, "breakeven after first partial")
            elif (target - close) / target * 100 <= Decimal(str(self.tm.near_target_pct)) and f"near_{label}" not in pos.alerted:
                pos.alerted.add(f"near_{label}")
                self.alerts.emit("TARGET_NEAR", symbol, f"₹{bar.close:.2f} is within {self.tm.near_target_pct}% of {label} ₹{target}: sell a portion near it", ts=bar.start)
        if pos.status != "open":
            return
        if ema9_ref:
            self._extension_check(pos, bar.close, ema9_ref, atr_ref, bar.start, key=f"ext_{day}")
        if upper is not None and float(upper) > 0:
            gap = (float(upper) - bar.close) / bar.close * 100
            if gap <= self.tm.upper_circuit_near_pct and f"uc_{day}" not in pos.alerted:
                pos.alerted.add(f"uc_{day}")
                self.alerts.emit("NEAR_UPPER_CIRCUIT", symbol, f"₹{bar.close:.2f} within {gap:.2f}% of the upper circuit ₹{upper}: consider trimming into strength", ts=bar.start)
        if lower is not None and bar.low <= float(lower) and f"lc_{day}" not in pos.alerted:
            pos.alerted.add(f"lc_{day}")
            self.alerts.emit("AT_LOWER_CIRCUIT", symbol, f"at the lower circuit ₹{lower}: a stop cannot fill while the stock is locked", level="critical", ts=bar.start)

    def _partial_qty(self, pos: Position, fraction: Decimal) -> int:
        qty = int((Decimal(pos.qty_initial) * fraction).to_integral_value(rounding=ROUND_FLOOR))
        qty = max(qty, 1) if pos.qty_open > 1 else 0
        return min(qty, pos.qty_open - 1)  # always keep shares to trail

    def _extension_check(self, pos: Position, price: float, ema9: float, atr_v: float | None, ts, key: str) -> None:
        if key in pos.alerted or ema9 <= 0:
            return
        ex = self.tm.extension
        pct = (price - ema9) / ema9 * 100
        atr_mult = (price - ema9) / atr_v if atr_v else 0.0
        hit_pct, hit_atr = pct >= ex.pct_above_ema, atr_mult >= ex.atr_multiple
        hit = {"either": hit_pct or hit_atr, "both": hit_pct and hit_atr, "pct": hit_pct, "atr": hit_atr}[ex.mode]
        if hit:
            pos.alerted.add(key)
            self.alerts.emit("EXTENDED", pos.symbol, f"₹{price:.2f} is {pct:.1f}% / {atr_mult:.1f} ATR above the {ex.ema} EMA ₹{ema9:.2f}: consider trimming into strength", ts=ts)

    def _move_stop(self, pos: Position, new_stop: Decimal, ts, reason: str) -> None:
        if pos.stop_order is not None and pos.stop_order.is_open:
            self.broker.cancel(pos.stop_order.order_id, ts)
        pos.stop = new_stop
        if pos.stop_kind == "GTT":
            pos.stop_order = self.orders.place_gtt_stop(pos.symbol, pos.qty_open, new_stop, ts, pos.plan.product)
        else:
            pos.stop_order = self.orders.place_stop(pos.symbol, pos.qty_open, new_stop, pos.tick, ts, pos.plan.product)
        self.alerts.emit("STOP_MOVED", pos.symbol, f"stop moved to ₹{new_stop} ({reason})", ts=ts)

    # ------------------------------------------------------------------ session boundaries
    def end_of_continuous_session(self, symbol: str, ts: datetime) -> None:
        """Continuous trading over for this stock: DAY stops expire; protect the overnight hold."""
        pos = self.positions.get(symbol)
        expired = self.broker.expire_day_orders(symbol, ts, pos.cas_stock if pos else False) if hasattr(self.broker, "expire_day_orders") else []
        if pos is None or pos.status != "open":
            return
        for o in expired:
            if pos.stop_order is not None and o.order_id == pos.stop_order.order_id:
                self.alerts.emit("STOP_EXPIRED", symbol, f"DAY stop ₹{pos.stop} {o.note}", ts=ts)
                pos.stop_order, pos.stop_kind = None, None
        if pos.stop_kind == "GTT" and pos.stop_order is not None and pos.stop_order.is_open:
            return
        if self.tm.overnight_stop.method == "gtt" and getattr(self.broker, "supports_gtt", False) and self.orders.mode != "alerts":
            gtt = self.orders.place_gtt_stop(symbol, pos.qty_open, pos.stop, ts, pos.plan.product)
            if gtt is not None:
                pos.stop_order, pos.stop_kind = gtt, "GTT"
                pos.flags.discard(REPLACE_FLAG)
                self.alerts.emit(
                    "OVERNIGHT_GTT", symbol,
                    f"held overnight: GTT stop SELL {pos.qty_open} below ₹{pos.stop} is active (Upstox places it as a LIMIT when triggered; automated sells need DDPI or eDIS/TPIN)",
                    ts=ts,
                )
                return
        pos.flags.add(REPLACE_FLAG)
        self.alerts.emit("REPLACE_SL", symbol, f"held overnight WITHOUT a resting stop: re-place SL ₹{pos.stop} for {pos.qty_open} shares at {self.tm.overnight_stop.replace_time:%H:%M} next session", level="warning", ts=ts)

    def overnight_compliance(self, ts: datetime) -> dict[str, str]:
        """After the close every open delivery position must have an active GTT stop or the re-place flag."""
        out: dict[str, str] = {}
        for sym, pos in self.positions.items():
            if pos.status != "open" or pos.plan.product != "CNC":
                continue
            if pos.stop_kind == "GTT" and pos.stop_order is not None and pos.stop_order.is_open:
                out[sym] = "GTT stop active"
            elif REPLACE_FLAG in pos.flags:
                out[sym] = f"flagged: re-place SL ₹{pos.stop} at {self.tm.overnight_stop.replace_time:%H:%M}"
            else:
                out[sym] = "UNPROTECTED"
                self.alerts.emit("UNPROTECTED", sym, "open delivery position with neither a GTT stop nor a re-place flag", level="critical", ts=ts)
        return out

    def start_of_session(self, symbol: str, session: "Session", open_bar: "Bar") -> None:
        pos = self.positions.get(symbol)
        if pos is None or pos.status != "open":
            return
        if pos.pending_exit:
            reason, pos.pending_exit = pos.pending_exit, None
            fills = self.orders.exit(symbol, pos.qty_open, open_bar.start, reason, reference_price=to_decimal(open_bar.open))
            self.on_fills(fills)
            if pos.status != "open":
                return
        if REPLACE_FLAG in pos.flags:
            pos.stop_order = self.orders.place_stop(symbol, pos.qty_open, pos.stop, pos.tick, open_bar.start, pos.plan.product)
            pos.stop_kind = "DAY_SL" if pos.stop_order else None
            if pos.stop_order:
                pos.flags.discard(REPLACE_FLAG)

    def end_of_day(self, symbol: str, daily: pd.DataFrame, ts: datetime) -> None:
        """After the close: 9-EMA trailing exit and the extension check on today's daily bar."""
        pos = self.positions.get(symbol)
        if pos is None or pos.status != "open" or daily.empty:
            return
        tr = self.tm.trail
        e = ema(daily["close"], tr.ema)
        close = float(daily["close"].iloc[-1])
        active = pos.trail_active or tr.activation == "entry"
        if active:
            broke = close < float(e.iloc[-1]) if tr.rule == "close_below_ema" else (len(daily) > 1 and close < float(daily["low"].iloc[-2]))
            if broke:
                what = f"daily close ₹{close:.2f} below the {tr.ema} EMA ₹{float(e.iloc[-1]):.2f}" if tr.rule == "close_below_ema" else f"daily close ₹{close:.2f} below the previous low"
                if tr.exit_timing == "same_close":
                    fills = self.orders.exit(symbol, pos.qty_open, ts, f"trail: {what}", reference_price=to_decimal(close))
                    self.on_fills(fills)
                else:
                    pos.pending_exit = f"trail: {what}"
                    self.alerts.emit("TRAIL_EXIT", symbol, f"{what}: sell the remaining {pos.qty_open} at the next open", level="warning", ts=ts)
        if pos.status == "open":
            a = atr(daily, self.tm.extension.atr_period)
            ext_ema = ema(daily["close"], self.tm.extension.ema)
            self._extension_check(pos, close, float(ext_ema.iloc[-1]), float(a.iloc[-1]), ts, key=f"ext_close_{to_ist(ts).date()}")

    # ------------------------------------------------------------------ manual override (Stage 4.5)
    def set_hold_full(self, symbol: str, on: bool, ts: datetime | None = None) -> None:
        if not self.tm.hold_full_override.allowed:
            raise ValueError("hold-full override is disabled in settings")
        pos = self.positions[symbol]
        pos.hold_full = on
        self.alerts.emit("HOLD_FULL", symbol, f"hold-full override {'ON: partials skipped' if on else 'OFF'}", ts=ts)

    def hold_full_hint(self, symbol: str, benchmark: pd.DataFrame | None, last_prices: dict[str, float]) -> tuple[bool, list[str]]:
        cfg = self.tm.hold_full_override
        reasons: list[str] = []
        strong = False
        if benchmark is not None and len(benchmark) > cfg.market_strong_ema + 5:
            e = ema(benchmark["close"], cfg.market_strong_ema)
            slope = float(slope_pct_per_day(e, 5).iloc[-1])
            above = float(benchmark["close"].iloc[-1]) > float(e.iloc[-1])
            strong = above and slope >= 0
            reasons.append(f"market {'strong' if strong else 'not strong'}: {cfg.benchmark} {'above' if above else 'below'} its {cfg.market_strong_ema} EMA, slope {slope:+.2f}%/day")
        else:
            reasons.append("no benchmark data: market strength unknown")
        others = [p for s, p in self.positions.items() if s != symbol and p.status == "open"]
        cushions = True
        for p in others:
            r = p.unrealized_r(last_prices.get(p.symbol, float(p.entry_price)))
            ok = r >= Decimal(str(cfg.min_cushion_r))
            cushions &= ok
            reasons.append(f"{p.symbol} {r:+}R {'≥' if ok else '<'} {cfg.min_cushion_r}R cushion")
        if not others:
            reasons.append("no other open positions")
        return strong and cushions, reasons

