"""Paper broker: simulated fills that respect how NSE orders really behave.

* Buy entries fill at the trigger reference price + ``slippage_ticks`` (never above the limit).
* SL-M sells trigger when a bar trades at or below the trigger and fill at the
  trigger, or at the bar's open when it **gaps below** (a worse price), minus slippage.
* While a stock is **locked at the lower circuit** (the whole bar sits at the
  limit) no sell can fill; the order waits and an alert explains why.
* DAY orders expire when continuous trading ends: 15:30, or 15:15 for CAS (F&O)
  stocks, where the exchange cancels pending SL orders at the auction transition.
* GTT stops persist across sessions. With ``gtt_fill_model: limit_at_trigger``
  a triggered GTT becomes a LIMIT sell at the trigger price (Upstox behaviour):
  if the open gaps below it, the limit rests unfilled until price returns.

OHLC ordering inside a bar follows the replay convention: stops are checked
before targets in the same bar (the conservative assumption).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from nseswing.core.ticks import round_to_tick, to_decimal
from nseswing.core.timeutil import to_ist
from nseswing.orders.base import Fill, Order, OrderType, Side, Status

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings
    from nseswing.intraday.bars import Bar


class PaperBroker:
    name = "paper"
    supports_gtt = True

    def __init__(self, settings: "Settings"):
        self.s = settings
        self.cfg = settings.execution.paper
        self.orders: dict[str, Order] = {}
        self.ticks: dict[str, Decimal] = {}
        self.notes: list[tuple[pd.Timestamp, str, str]] = []  # (ts, symbol, message) for the alert layer

    def set_tick(self, symbol: str, tick: Decimal) -> None:
        self.ticks[symbol] = tick

    def tick(self, symbol: str) -> Decimal:
        return self.ticks.get(symbol, Decimal("0.05"))

    # ------------------------------------------------------------------ gateway API
    def place(self, order: Order, ts: datetime, reference_price: Decimal | None = None) -> list[Fill]:
        order.placed_at = to_ist(ts)
        self.orders[order.order_id] = order
        tick = self.tick(order.symbol)
        slip = tick * self.cfg.slippage_ticks
        if reference_price is None or order.order_type in (OrderType.SL, OrderType.SL_M, OrderType.GTT):
            return []
        ref = to_decimal(reference_price)
        if order.side == Side.BUY:
            px = round_to_tick(ref + slip, tick, "up")
            if order.order_type == OrderType.LIMIT and order.price is not None:
                if ref > order.price:
                    return []  # price ran above the limit: resting
                px = min(px, order.price)
            return [self._fill(order, px, ts)]
        # sells with a known price (exit at open/close, or a target already traded)
        px = round_to_tick(ref - slip, tick, "down") if order.order_type == OrderType.MARKET else ref
        if order.order_type == OrderType.LIMIT and order.price is not None:
            if ref < order.price:
                return []
            px = max(order.price, round_to_tick(ref, tick, "down"))
        return [self._fill(order, px, ts)]

    def cancel(self, order_id: str, ts: datetime | None = None) -> None:
        o = self.orders.get(order_id)
        if o and o.is_open:
            o.status = Status.CANCELLED

    def modify_qty(self, order_id: str, qty: int) -> None:
        o = self.orders.get(order_id)
        if o and o.is_open:
            if qty <= 0:
                o.status = Status.CANCELLED
            o.qty = qty

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self.orders.values() if o.is_open and (symbol is None or o.symbol == symbol)]

    # ------------------------------------------------------------------ simulation
    def on_bar(self, symbol: str, bar: "Bar", continuous: bool, lower_circuit: Decimal | None = None) -> list[Fill]:
        """Process resting orders of ``symbol`` against one bar (intraday or daily)."""
        if not continuous:
            return []
        fills: list[Fill] = []
        tick = self.tick(symbol)
        slip = tick * self.cfg.slippage_ticks
        o_, h_, l_ = to_decimal(bar.open), to_decimal(bar.high), to_decimal(bar.low)
        locked = lower_circuit is not None and h_ <= lower_circuit + tick * self.s.price_bands.locked_tolerance_ticks
        # stops first, then everything else (conservative ordering)
        resting = sorted(self.open_orders(symbol), key=lambda o: 0 if o.purpose == "stop" else 1)
        for o in resting:
            if not o.is_open:
                continue
            if o.side == Side.SELL and o.order_type in (OrderType.SL_M, OrderType.SL, OrderType.GTT) and o.status == Status.PENDING:
                if l_ > o.trigger_price:
                    continue
                if locked:
                    self.notes.append((bar.start, symbol, f"stop ₹{o.trigger_price} triggered but the stock is LOCKED at the lower circuit ₹{lower_circuit}: no buyers, the sell cannot fill"))
                    continue
                if o.order_type == OrderType.SL_M or (o.order_type == OrderType.GTT and self.cfg.gtt_fill_model == "market"):
                    base = min(o_, o.trigger_price)
                    px = max(l_, round_to_tick(base - slip, tick, "down"))
                    if o_ < o.trigger_price:
                        self.notes.append((bar.start, symbol, f"gapped below the stop: opened ₹{o_} vs stop ₹{o.trigger_price}; filled ₹{px}"))
                    fills.append(self._fill(o, px, bar.start))
                    continue
                # SL (stop-limit) or GTT placed as a limit at its trigger price
                limit = o.price if o.price is not None else o.trigger_price
                if o_ >= o.trigger_price:  # traded down through the trigger: limit is marketable
                    px = o.trigger_price if o.order_type == OrderType.GTT else max(limit, round_to_tick(o.trigger_price - slip, tick, "down"))
                    fills.append(self._fill(o, px, bar.start))
                elif o_ >= limit:  # gapped below the trigger but not below the limit
                    px = max(limit, round_to_tick(o_ - slip, tick, "down"))
                    self.notes.append((bar.start, symbol, f"gapped below the stop: opened ₹{o_} vs stop ₹{o.trigger_price}; filled ₹{px}"))
                    fills.append(self._fill(o, px, bar.start))
                else:  # gapped below the limit: the sell rests unfilled
                    o.status = Status.TRIGGERED
                    self.notes.append((bar.start, symbol, f"{o.order_type.value} stop triggered at ₹{o.trigger_price} but its LIMIT ₹{limit} is above the market (opened ₹{o_}): sell order resting UNFILLED"))
                    if h_ >= limit:
                        fills.append(self._fill(o, limit, bar.start))
                continue
            if o.side == Side.SELL and (o.status == Status.TRIGGERED or o.order_type == OrderType.LIMIT):
                limit = o.price if o.price is not None else o.trigger_price
                if h_ >= limit and not locked:
                    fills.append(self._fill(o, max(limit, round_to_tick(o_, tick, "down")) if o_ > limit else limit, bar.start))
                continue
            if o.order_type == OrderType.MARKET:
                if o.side == Side.SELL:
                    if locked:
                        continue
                    fills.append(self._fill(o, max(l_, round_to_tick(o_ - slip, tick, "down")), bar.start))
                else:
                    fills.append(self._fill(o, min(h_, round_to_tick(o_ + slip, tick, "up")), bar.start))
                continue
            if o.side == Side.BUY and o.order_type == OrderType.LIMIT and l_ <= o.price:
                fills.append(self._fill(o, min(o_, o.price), bar.start))
        return fills

    def expire_day_orders(self, symbol: str, ts: datetime, cas_stock: bool) -> list[Order]:
        out = []
        for o in self.open_orders(symbol):
            if o.validity == "DAY":
                o.status = Status.EXPIRED
                o.note = "cancelled by the exchange at the closing-auction transition" if (cas_stock and o.order_type in (OrderType.SL, OrderType.SL_M)) else "DAY order expired at the close"
                out.append(o)
        return out

    def _fill(self, order: Order, price: Decimal, ts) -> Fill:
        order.status = Status.FILLED
        order.filled_qty = order.qty
        order.avg_price = price
        order.filled_at = to_ist(ts)
        return Fill(order.order_id, order.symbol, order.side, order.qty, price, to_ist(ts), order.purpose)

    # ------------------------------------------------------------------ persistence
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for o in self.orders.values():
            d = asdict(o)
            d.update(side=o.side.value, order_type=o.order_type.value, status=o.status.value)
            rows.append(d)
        path.write_text(json.dumps(rows, default=str, indent=1), encoding="utf-8")

    def load(self, path: Path) -> None:
        if not path.exists():
            return
        for d in json.loads(path.read_text(encoding="utf-8")):
            for k in ("price", "trigger_price", "avg_price"):
                d[k] = Decimal(d[k]) if d.get(k) not in (None, "None") else None
            for k in ("placed_at", "filled_at"):
                d[k] = pd.Timestamp(d[k]) if d.get(k) not in (None, "None") else None
            d["side"], d["order_type"], d["status"] = Side(d["side"]), OrderType(d["order_type"]), Status(d["status"])
            o = Order(**d)
            self.orders[o.order_id] = o
