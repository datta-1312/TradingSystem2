from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from nseswing.alerts import AlertBus
from nseswing.config import load_settings
from nseswing.core.timeutil import IST
from nseswing.intraday.bars import Bar
from nseswing.journal import Journal
from nseswing.orders.base import MissingStop, Order, OrderManager, OrderType, Side, Status
from nseswing.orders.paper import PaperBroker
from nseswing.orders.upstox_live import (
    LiveTradingLocked,
    UpstoxLiveGateway,
    assert_live_allowed,
    gtt_entry_with_stop_payload,
    gtt_stop_payload,
    order_payload,
)
from nseswing.risk import RiskCalculator
from nseswing.trademanager import REPLACE_FLAG, TradeManager

D = Decimal
DAY = date(2026, 11, 3)  # Tuesday
NEXT = date(2026, 11, 4)


def ts(d, hhmm):
    return pd.Timestamp(f"{d} {hhmm}", tz=IST)


def bar(d, hhmm, o, h, l, c, minutes=1):
    start = ts(d, hhmm)
    return Bar(start, start + pd.Timedelta(minutes=minutes), o, h, l, c, 5000.0)


class Rig:
    def __init__(self, calendar, **overrides):
        self.s = load_settings(overrides=overrides) if overrides else load_settings()
        self.cal = calendar
        self.alerts = AlertBus(self.s, console=False, file=None)
        self.broker = PaperBroker(self.s)
        self.broker.set_tick("ACC", D("0.05"))
        self.journal = Journal(":memory:")
        self.om = OrderManager(self.s, self.broker if self.s.execution.mode != "alerts" else None, self.alerts)
        self.tm = TradeManager(self.s, self.om, self.alerts, calendar, self.journal)
        self.plan = RiskCalculator(self.s).plan("ACC", D("500.00"), D("487.50"), tick=D("0.05"))

    def enter(self, cas=False):
        return self.tm.enter(self.plan, ts(DAY, "10:05"), D("500.00"), cas_stock=cas)

    def feed(self, b, cas=False, lower=None):
        sess = self.cal.session(b.start.date())
        fills = self.broker.on_bar("ACC", b, sess.is_continuous(b.start, cas), lower)
        self.tm.on_fills(fills)
        self.tm.on_bar("ACC", b)
        return fills


# ------------------------------------------------------------------ stop attached to every entry
def test_entry_without_stop_is_refused(calendar):
    rig = Rig(calendar)
    rig.plan.stop = rig.plan.entry
    with pytest.raises(MissingStop):
        rig.om.submit_entry(rig.plan, ts(DAY, "10:05"), D("500.00"))


def test_paper_entry_places_stop_immediately_after_fill(calendar):
    rig = Rig(calendar)
    pos = rig.enter()
    assert pos.entry_price == D("500.05")  # 1 tick of slippage
    kinds = rig.alerts.kinds()
    assert kinds.index("ENTRY_FILLED") < kinds.index("STOP_PLACED")
    stop = rig.broker.open_orders("ACC")[0]
    assert stop.order_type == OrderType.SL_M and stop.trigger_price == D("487.50") and stop.qty == 400
    assert pos.targets["3R"] == D("537.70")  # recomputed from the actual fill: R = ₹12.55


def test_alerts_mode_sends_nothing(calendar):
    rig = Rig(calendar, execution={"mode": "alerts"})
    assert rig.enter() is None
    assert rig.alerts.kinds() == ["ENTRY_SIGNAL"] and rig.broker.orders == {}


# ------------------------------------------------------------------ paper fills
def test_gap_down_fills_below_stop(calendar):
    rig = Rig(calendar)
    rig.enter()
    rig.tm.end_of_continuous_session("ACC", ts(DAY, "15:30"))  # GTT overnight
    fills = rig.feed(bar(NEXT, "09:15", 480.0, 482.0, 478.0, 481.0), cas=False)
    # GTT limit-at-trigger (Upstox) cannot fill on a gap below ₹487.50
    assert fills == [] and rig.tm.positions["ACC"].stop_order.status == Status.TRIGGERED
    assert "STOP_UNFILLED" in rig.alerts.kinds()
    fills = rig.feed(bar(NEXT, "09:16", 481.0, 488.0, 480.5, 487.0))
    assert fills and fills[0].price == D("487.50")


def test_slm_gap_down_fills_at_open(calendar):
    rig = Rig(calendar, trade_management={"overnight_stop": {"method": "replace_next_morning"}})
    rig.enter()
    rig.tm.end_of_continuous_session("ACC", ts(DAY, "15:30"))
    rig.tm.start_of_session("ACC", calendar.session(NEXT), bar(NEXT, "09:15", 480.0, 482.0, 478.0, 481.0))
    fills = rig.feed(bar(NEXT, "09:15", 480.0, 482.0, 478.0, 481.0))
    assert fills[0].price == D("479.95")  # opened ₹480 under the ₹487.50 stop, less 1 tick
    res = rig.journal.result(rig.tm.positions["ACC"].trade_id)
    assert res.r_gross < D("-1.5")  # the real loss exceeded 1R


def test_lower_circuit_lock_blocks_the_stop(calendar):
    rig = Rig(calendar)
    rig.enter()
    lc = D("450.00")
    fills = rig.feed(bar(DAY, "11:00", 450.0, 450.0, 450.0, 450.0), lower=lc)
    assert fills == [] and "LOCKED" in rig.broker.notes[-1][2]
    fills = rig.feed(bar(DAY, "11:01", 450.0, 452.0, 450.0, 451.0), lower=lc)
    assert fills and fills[0].price <= D("452.00")


def test_cas_stock_stop_cancelled_at_1515(calendar):
    rig = Rig(calendar)
    rig.enter(cas=True)
    rig.tm.end_of_continuous_session("ACC", ts(DAY, "15:15"))
    assert any("closing-auction" in a.message for a in rig.alerts.of_kind("STOP_EXPIRED"))
    assert rig.tm.positions["ACC"].stop_kind == "GTT"


# ------------------------------------------------------------------ Stage 4 management
def test_partials_at_3r_and_5r_then_trailing_exit(calendar):
    rig = Rig(calendar)
    pos = rig.enter()
    rig.feed(bar(DAY, "13:00", 530.0, 537.75, 529.0, 536.0))  # 3R ₹537.70 hit
    assert pos.qty_open == 268 and "3R" in pos.partials_done and pos.trail_active
    assert rig.broker.open_orders("ACC")[0].qty == 268  # stop quantity follows the position
    rig.feed(bar(DAY, "14:00", 555.0, 562.95, 554.0, 560.0))  # 5R ₹562.80
    assert pos.qty_open == 136
    # daily close below the 9 EMA → sell the rest at the next open
    idx = pd.bdate_range("2026-10-01", periods=24)
    closes = [500 + i * 3 for i in range(23)] + [540.0]
    daily = pd.DataFrame({"open": closes, "high": [c + 2 for c in closes], "low": [c - 2 for c in closes], "close": closes, "volume": 1e5}, index=idx)
    rig.tm.end_of_day("ACC", daily, ts(DAY, "15:35"))
    assert pos.pending_exit and "TRAIL_EXIT" in rig.alerts.kinds()
    rig.tm.end_of_continuous_session("ACC", ts(DAY, "15:30"))
    rig.tm.start_of_session("ACC", calendar.session(NEXT), bar(NEXT, "09:15", 545.0, 546.0, 544.0, 545.5))
    assert pos.status == "closed"
    res = rig.journal.result(pos.trade_id)
    assert res.r_gross > D("3") and res.r_net < res.r_gross


def test_hold_full_override_skips_partials(calendar):
    rig = Rig(calendar)
    pos = rig.enter()
    rig.tm.set_hold_full("ACC", True)
    rig.feed(bar(DAY, "13:00", 530.0, 538.0, 529.0, 537.0))
    assert pos.qty_open == 400 and pos.trail_active and "HOLD_FULL" in rig.alerts.kinds()


def test_extension_and_upper_circuit_alerts(calendar):
    rig = Rig(calendar)
    rig.enter()
    b = bar(DAY, "12:00", 520.0, 524.0, 519.0, 523.0)
    rig.tm.on_bar("ACC", b, ema9_ref=470.0, atr_ref=10.0, upper=D("525.00"))
    kinds = rig.alerts.kinds()
    assert "EXTENDED" in kinds and "NEAR_UPPER_CIRCUIT" in kinds


def test_hold_full_hint(calendar):
    rig = Rig(calendar)
    rig.enter()
    idx = pd.bdate_range("2026-08-03", periods=60)
    bench = pd.DataFrame({"close": [24000 + 10 * i for i in range(60)]}, index=idx, dtype=float)
    ok, reasons = rig.tm.hold_full_hint("OTHER", bench, {"ACC": 530.0})
    assert ok and any("strong" in r for r in reasons)
    ok, _ = rig.tm.hold_full_hint("OTHER", bench, {"ACC": 505.0})
    assert not ok  # ACC is only +0.4R


def test_same_day_exit_is_taxed_as_intraday(calendar):
    rig = Rig(calendar)
    pos = rig.enter()
    rig.feed(bar(DAY, "11:00", 488.0, 489.0, 487.0, 487.2))  # stopped out the same day
    fills = rig.journal.fills(pos.trade_id)
    assert "intraday-tax-adjustment" in set(fills["purpose"])
    sell = fills[fills["side"] == "SELL"].iloc[0]
    assert Decimal(sell["charges_total"]) < D("150")  # no delivery STT / DP on a same-day sell


# ------------------------------------------------------------------ live gateway: locked and payloads
def test_live_is_locked_by_default(settings):
    with pytest.raises(LiveTradingLocked):
        assert_live_allowed(settings)
    s = load_settings(overrides={"execution": {"mode": "live", "live": {"enabled": True, "confirm_live_trading": True}}})
    with pytest.raises(LiveTradingLocked, match="static IP"):
        assert_live_allowed(s)


def test_upstox_payloads(settings):
    o = Order("ACC", Side.SELL, 400, OrderType.SL_M, "stop", trigger_price=D("487.50"), tag="nseswing")
    body = order_payload(o, "NSE_EQ|INE000A01010", settings)
    assert body["product"] == "D" and body["order_type"] == "SL-M" and body["trigger_price"] == 487.5 and body["price"] == 0
    g = gtt_stop_payload("NSE_EQ|INE000A01010", 268, D("487.50"))
    assert g["type"] == "SINGLE" and g["transaction_type"] == "SELL"
    assert g["rules"] == [{"strategy": "ENTRY", "trigger_type": "BELOW", "trigger_price": 487.5}]
    m = gtt_entry_with_stop_payload("NSE_EQ|INE000A01010", 400, D("500.15"), D("487.50"))
    assert [r["strategy"] for r in m["rules"]] == ["ENTRY", "STOPLOSS"] and m["rules"][1]["trigger_type"] == "IMMEDIATE"


class FakeClient:
    def __init__(self):
        self.placed = []

    def place_order(self, body):
        self.placed.append(body)
        return {"status": "success", "data": {"order_id": "250101000000001"}}

    def order_details(self, oid):
        return {"status": "success", "data": {"status": "complete", "filled_quantity": 400, "average_price": 500.1}}

    def place_gtt(self, body):
        self.placed.append(body)
        return {"status": "success", "data": {"gtt_order_ids": ["GTT-1"]}}


def test_live_gateway_with_all_switches_on():
    s = load_settings(overrides={"execution": {"mode": "live", "live": {"enabled": True, "confirm_live_trading": True, "static_ip_registered": True}}})
    gw = UpstoxLiveGateway(s, FakeClient(), {"ACC": "NSE_EQ|INE000A01010"})
    o = Order("ACC", Side.BUY, 400, OrderType.LIMIT, "entry", price=D("500.15"))
    fills = gw.place(o, datetime(2026, 11, 3, 10, 5, tzinfo=IST))
    assert o.broker_ref == "250101000000001" and fills[0].price == D("500.1")
    g = Order("ACC", Side.SELL, 400, OrderType.GTT, "stop", trigger_price=D("487.50"), validity="GTT")
    gw.place(g, datetime(2026, 11, 3, 15, 31, tzinfo=IST))
    assert g.broker_ref == "GTT-1"
