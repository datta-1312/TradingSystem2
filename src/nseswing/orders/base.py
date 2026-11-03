"""Order model, broker gateway protocol and the stop-attached order flow.

Safety rules enforced here, whatever the gateway:

* **No entry without a stop.** ``submit_entry`` raises :class:`MissingStop`
  unless the plan has a stop below the entry and at least one share.
* **The stop goes in as soon as the entry fills** (DAY stop-loss, or a GTT if
  the broker flow attaches it). If the stop is rejected a CRITICAL alert is
  raised and, with ``on_stop_failure: alert_and_flatten``, the position is sold.
* **Alerts mode sends nothing**: it only announces what it would have done.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Protocol

import pandas as pd

from nseswing.core.ticks import round_to_tick

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.alerts import AlertBus
    from nseswing.config import Settings
    from nseswing.risk import TradePlan


class MissingStop(ValueError):
    pass


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"
    GTT = "GTT"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Status(str, Enum):
    PENDING = "pending"  # open / trigger pending
    TRIGGERED = "triggered"  # GTT fired; its limit order is resting unfilled
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


_ids = itertools.count(1)


@dataclass
class Order:
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    purpose: str  # entry | stop | target | exit
    product: str = "CNC"
    price: Decimal | None = None  # limit price
    trigger_price: Decimal | None = None
    validity: str = "DAY"  # DAY | GTT
    tag: str = "nseswing"
    order_id: str = field(default_factory=lambda: f"N{next(_ids):06d}")
    status: Status = Status.PENDING
    placed_at: pd.Timestamp | None = None
    filled_qty: int = 0
    avg_price: Decimal | None = None
    filled_at: pd.Timestamp | None = None
    broker_ref: str | None = None
    note: str = ""

    @property
    def is_open(self) -> bool:
        return self.status in (Status.PENDING, Status.TRIGGERED)


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: Side
    qty: int
    price: Decimal
    ts: pd.Timestamp
    purpose: str


class Gateway(Protocol):
    name: str
    supports_gtt: bool

    def place(self, order: Order, ts: datetime, reference_price: Decimal | None = None) -> list[Fill]: ...

    def cancel(self, order_id: str, ts: datetime) -> None: ...

    def modify_qty(self, order_id: str, qty: int) -> None: ...

    def open_orders(self, symbol: str | None = None) -> list[Order]: ...


@dataclass
class EntryTicket:
    plan: "TradePlan"
    entry: Order | None
    fill: Fill | None
    stop: Order | None
    alert_only: bool = False


class OrderManager:
    def __init__(self, settings: "Settings", gateway: Gateway | None, alerts: "AlertBus"):
        self.s = settings
        self.gw = gateway
        self.alerts = alerts

    @property
    def mode(self) -> str:
        return self.s.execution.mode

    # ------------------------------------------------------------------ entry
    def submit_entry(self, plan: "TradePlan", ts: datetime, reference_price: Decimal) -> EntryTicket:
        if plan.stop is None or plan.stop >= plan.entry:
            raise MissingStop(f"{plan.symbol}: refusing an entry without a valid stop below the entry")
        if plan.shares <= 0:
            raise MissingStop(f"{plan.symbol}: plan has no shares (risk per share too large)")
        if self.mode == "alerts" or self.gw is None:
            self.alerts.emit(
                "ENTRY_SIGNAL", plan.symbol,
                f"alerts mode, no order sent: BUY {plan.shares} @ ≈₹{plan.entry}, then SL ₹{plan.stop} immediately after the fill",
                level="warning", ts=ts,
            )
            return EntryTicket(plan, None, None, None, alert_only=True)
        live = self.s.execution.live
        buffer = plan.tick * live.entry_limit_buffer_ticks
        order = Order(
            plan.symbol, Side.BUY, plan.shares,
            OrderType.LIMIT if live.entry_order_type == "LIMIT" else OrderType.MARKET,
            "entry", product=plan.product,
            price=round_to_tick(reference_price + buffer, plan.tick, "up") if live.entry_order_type == "LIMIT" else None,
            tag=live.order_tag,
        )
        fills = self.gw.place(order, ts, reference_price=reference_price)
        fill = next((f for f in fills if f.order_id == order.order_id), None)
        if fill is None:
            self.alerts.emit("ENTRY_PENDING", plan.symbol, f"buy order {order.order_id} not filled yet; the stop is placed the moment it fills", ts=ts)
            return EntryTicket(plan, order, None, None)
        self.alerts.emit("ENTRY_FILLED", plan.symbol, f"bought {fill.qty} @ ₹{fill.price} ({self.gw.name})", ts=fill.ts, order_id=order.order_id)
        stop = self.place_stop(plan.symbol, fill.qty, plan.stop, plan.tick, fill.ts, plan.product)
        return EntryTicket(plan, order, fill, stop)

    # ------------------------------------------------------------------ stops
    def place_stop(self, symbol: str, qty: int, stop: Decimal, tick: Decimal, ts: datetime, product: str = "CNC") -> Order | None:
        live = self.s.execution.live
        if live.stop_order_type == "SL":
            limit = round_to_tick(stop * (1 - live.stop_limit_buffer_pct / Decimal(100)), tick, "down")
            order = Order(symbol, Side.SELL, qty, OrderType.SL, "stop", product=product, price=limit, trigger_price=stop, tag=live.order_tag)
        else:
            order = Order(symbol, Side.SELL, qty, OrderType.SL_M, "stop", product=product, trigger_price=stop, tag=live.order_tag)
        try:
            self.gw.place(order, ts)
        except Exception as exc:  # noqa: BLE001 - must alert on any failure
            order.status, order.note = Status.REJECTED, str(exc)
        if order.status == Status.REJECTED:
            self.alerts.emit("STOP_FAILED", symbol, f"stop order REJECTED ({order.note}): the position is unprotected", level="critical", ts=ts)
            if live.on_stop_failure == "alert_and_flatten":
                self.exit(symbol, qty, ts, "flatten: stop could not be placed")
            return None
        self.alerts.emit("STOP_PLACED", symbol, f"{order.order_type.value} SELL {qty} trigger ₹{stop} (DAY) placed right after the fill", ts=ts, order_id=order.order_id)
        return order

    def place_gtt_stop(self, symbol: str, qty: int, stop: Decimal, ts: datetime, product: str = "CNC") -> Order | None:
        if not self.gw or not self.gw.supports_gtt:
            return None
        order = Order(symbol, Side.SELL, qty, OrderType.GTT, "stop", product=product, trigger_price=stop, price=stop, validity="GTT", tag=self.s.execution.live.order_tag)
        self.gw.place(order, ts)
        if order.status == Status.REJECTED:
            self.alerts.emit("GTT_FAILED", symbol, f"GTT stop rejected ({order.note})", level="critical", ts=ts)
            return None
        return order

    def exit(self, symbol: str, qty: int, ts: datetime, reason: str, limit: Decimal | None = None, reference_price: Decimal | None = None, purpose: str = "exit") -> list[Fill]:
        if self.mode == "alerts" or self.gw is None:
            self.alerts.emit("EXIT_SIGNAL", symbol, f"alerts mode, no order sent: SELL {qty} ({reason})", level="warning", ts=ts)
            return []
        order = Order(symbol, Side.SELL, qty, OrderType.LIMIT if limit is not None else OrderType.MARKET, purpose, price=limit, tag=self.s.execution.live.order_tag, note=reason)
        return self.gw.place(order, ts, reference_price=reference_price)
