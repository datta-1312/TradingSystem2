"""Upstox live order gateway: locked unless you explicitly enable live mode.

Live orders need ALL of these in ``settings.yaml``:

    execution.mode: live
    execution.live.enabled: true
    execution.live.confirm_live_trading: true
    execution.live.static_ip_registered: true   # SEBI retail-algo framework (from 1 Apr 2026)

Orders are rate-limited to ``max_orders_per_second`` (keep it under the 10
orders/sec exchange registration threshold) and tagged with ``order_tag``. If
your broker requires an algo identifier header, set ``algo_header_name/value``.

Payload formats follow the Upstox v2/v3 documentation as published; they were
unit-tested against these builders only (no live endpoint was reachable while
building). Place a small test order by hand and compare before trusting it.

GTT notes (Upstox): every GTT needs exactly one ENTRY rule, TARGET/STOPLOSS
legs use trigger_type IMMEDIATE, and a triggered GTT is placed as a LIMIT order,
so a gap below the stop can leave it unfilled. A stop-only GTT on holdings is a
SINGLE SELL whose ENTRY rule triggers BELOW the stop price. Selling holdings
automatically needs DDPI (or a valid eDIS/TPIN authorisation) on your account.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nseswing.data.upstox import RateLimiter, UpstoxClient, UpstoxError
from nseswing.orders.base import Fill, Order, OrderType, Status

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings

PRODUCT_CODES = {"CNC": "D", "MIS": "I"}


class LiveTradingLocked(RuntimeError):
    pass


def assert_live_allowed(settings: "Settings") -> None:
    live = settings.execution.live
    if not settings.live_orders_allowed:
        raise LiveTradingLocked(
            "live orders are locked: set execution.mode: live, execution.live.enabled: true and "
            "execution.live.confirm_live_trading: true"
        )
    if not live.static_ip_registered:
        raise LiveTradingLocked("register your static IP with Upstox, then set execution.live.static_ip_registered: true")


def order_payload(order: Order, instrument_key: str, settings: "Settings") -> dict[str, Any]:
    body: dict[str, Any] = {
        "quantity": order.qty,
        "product": PRODUCT_CODES[order.product],
        "validity": "DAY",
        "price": float(order.price) if order.price is not None else 0,
        "tag": order.tag,
        "instrument_token": instrument_key,
        "order_type": order.order_type.value,
        "transaction_type": order.side.value,
        "disclosed_quantity": 0,
        "trigger_price": float(order.trigger_price) if order.trigger_price is not None else 0,
        "is_amo": False,
    }
    if settings.execution.live.use_hft_endpoint:
        body["slice"] = False
    return body


def gtt_stop_payload(instrument_key: str, qty: int, stop: Decimal, product: str = "CNC") -> dict[str, Any]:
    """Stop for shares already held: SINGLE SELL that fires when price goes BELOW the stop."""
    return {
        "type": "SINGLE",
        "quantity": qty,
        "product": PRODUCT_CODES[product],
        "rules": [{"strategy": "ENTRY", "trigger_type": "BELOW", "trigger_price": float(stop)}],
        "instrument_token": instrument_key,
        "transaction_type": "SELL",
    }


def gtt_entry_with_stop_payload(
    instrument_key: str, qty: int, entry_limit: Decimal, stop: Decimal, target: Decimal | None = None, product: str = "CNC"
) -> dict[str, Any]:
    """Buy now with the stop attached at the broker (GTT MULTIPLE: ENTRY + STOPLOSS [+ TARGET])."""
    rules = [
        {"strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": float(entry_limit)},
        {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": float(stop)},
    ]
    if target is not None:
        rules.append({"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": float(target)})
    return {"type": "MULTIPLE", "quantity": qty, "product": PRODUCT_CODES[product], "rules": rules, "instrument_token": instrument_key, "transaction_type": "BUY"}


class UpstoxLiveGateway:
    name = "upstox-live"
    supports_gtt = True

    def __init__(self, settings: "Settings", client: UpstoxClient, instrument_keys: dict[str, str]):
        assert_live_allowed(settings)
        self.s = settings
        self.client = client
        self.keys = instrument_keys
        self.orders: dict[str, Order] = {}
        rate = settings.execution.live.max_orders_per_second
        self.limiter = RateLimiter(per_second=rate, per_minute=rate * 60)

    def _key(self, symbol: str) -> str:
        if symbol not in self.keys:
            raise UpstoxError(f"no instrument key for {symbol}")
        return self.keys[symbol]

    def place(self, order: Order, ts: datetime, reference_price: Decimal | None = None) -> list[Fill]:
        assert_live_allowed(self.s)  # re-checked on every order
        self.limiter.acquire()
        try:
            if order.order_type == OrderType.GTT:
                resp = self.client.place_gtt(gtt_stop_payload(self._key(order.symbol), order.qty, order.trigger_price, order.product))
                ids = (resp.get("data") or {}).get("gtt_order_ids") or []
                order.broker_ref = ids[0] if ids else None
            else:
                resp = self.client.place_order(order_payload(order, self._key(order.symbol), self.s))
                data = resp.get("data") or {}
                order.broker_ref = data.get("order_id") or (data.get("order_ids") or [None])[0]
        except (UpstoxError, OSError) as exc:
            order.status, order.note = Status.REJECTED, str(exc)
            return []
        self.orders[order.order_id] = order
        return self.poll(order)

    def poll(self, order: Order) -> list[Fill]:
        """Ask Upstox for the order status; return a Fill once it is complete."""
        if order.order_type == OrderType.GTT or not order.broker_ref:
            return []
        data = (self.client.order_details(order.broker_ref).get("data") or {})
        status = str(data.get("status", "")).lower()
        if status == "complete":
            order.status = Status.FILLED
            order.filled_qty = int(data.get("filled_quantity") or order.qty)
            order.avg_price = Decimal(str(data.get("average_price")))
            return [Fill(order.order_id, order.symbol, order.side, order.filled_qty, order.avg_price, order.placed_at, order.purpose)]
        if status in ("rejected", "cancelled"):
            order.status = Status.REJECTED if status == "rejected" else Status.CANCELLED
            order.note = str(data.get("status_message") or status)
        return []

    def cancel(self, order_id: str, ts: datetime | None = None) -> None:
        o = self.orders.get(order_id)
        if not o or not o.broker_ref:
            return
        if o.order_type == OrderType.GTT:
            self.client.cancel_gtt(o.broker_ref)
        else:
            self.client.cancel_order(o.broker_ref)
        o.status = Status.CANCELLED

    def modify_qty(self, order_id: str, qty: int) -> None:
        o = self.orders.get(order_id)
        if not o or not o.broker_ref:
            return
        if o.order_type == OrderType.GTT:
            body = gtt_stop_payload(self._key(o.symbol), qty, o.trigger_price, o.product)
            body.pop("instrument_token")
            body.pop("transaction_type")
            body["gtt_order_id"] = o.broker_ref
            self.client.modify_gtt(body)
        else:
            self.client.request("PUT", "/v2/order/modify", body={"order_id": o.broker_ref, "quantity": qty, "validity": "DAY", "order_type": o.order_type.value, "price": float(o.price or 0), "trigger_price": float(o.trigger_price or 0), "disclosed_quantity": 0})
        o.qty = qty

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self.orders.values() if o.is_open and (symbol is None or o.symbol == symbol)]

