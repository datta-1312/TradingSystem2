"""The six acceptance tests from the specification, checked exactly.

1. Position size   4. Trigger fires / no trigger
2. R targets       5. Tick rounding before sizing
3. Higher lows     6. Overnight stop (GTT or re-place flag)
"""

import pandas as pd

from nseswing.screener import find_consolidation, higher_lows, higher_lows_check


# ------------------------------------------------------------------ 3. higher lows (Stage 1.7a)
def _base_with_lows(prior: float, current: float) -> pd.DataFrame:
    """Peak at bar 40, pullback low `prior` at bar 50, bounce, pullback low `current` at bar 66."""
    closes = []
    for i in range(100):
        if i <= 40:
            c = 400 + i * 3  # advance to 520
        elif i <= 50:
            c = 520 - (i - 40) * 6.5  # down to ~455
        elif i <= 58:
            c = 455 + (i - 50) * 6  # bounce to ~503
        elif i <= 66:
            c = 503 - (i - 58) * 3  # second pullback to ~479
        else:
            c = 479 + (i - 66) * 0.7
        closes.append(c)
    idx = pd.bdate_range("2026-05-04", periods=100)
    df = pd.DataFrame({"open": closes, "close": closes}, index=idx, dtype=float)
    df["high"] = df["close"] + 2.0
    df["low"] = df["close"] - 2.0
    df["volume"] = 500_000.0
    df.loc[idx[40], "high"] = 525.0
    df.loc[idx[50], "low"] = prior
    df.loc[idx[66], "low"] = current
    return df[["open", "high", "low", "close", "volume"]]


def test_acceptance_higher_lows_450_then_475_passes(settings):
    assert higher_lows(prior_low=450.0, latest_low=475.0) is True
    df = _base_with_lows(450.0, 475.0)
    chk = higher_lows_check(df, find_consolidation(df, settings), settings)
    assert chk.passed
    assert chk.details["prior_low"] == 450.0 and chk.details["latest_low"] == 475.0


def test_higher_lows_fails_when_current_low_is_lower(settings):
    assert higher_lows(prior_low=475.0, latest_low=450.0) is False
    df = _base_with_lows(475.0, 450.0)
    assert not higher_lows_check(df, find_consolidation(df, settings), settings).passed


# ------------------------------------------------------------------ 4. trigger (Stage 2.6 / 2.8)
def _engine(calendar, settings):
    from datetime import date
    from decimal import Decimal

    from nseswing.intraday.trigger import TriggerEngine
    from nseswing.levels import Level, Zone

    zone = Zone("ACC", Decimal("494.00"), Decimal("499.00"), [Level("avwap", 497.0, "AVWAP"), Level("ema21", 496.0, "EMA21")], "medium")
    return TriggerEngine("ACC", [zone], Decimal("0.05"), settings, calendar.session(date(2026, 11, 3)), stage1_pass=True)


def _bar(hhmm, o, h, l, c):
    from nseswing.core.timeutil import IST
    from nseswing.intraday.bars import Bar

    start = pd.Timestamp(f"2026-11-03 {hhmm}", tz=IST)
    return Bar(start, start + pd.Timedelta(minutes=5), o, h, l, c, 25_000.0)


def test_acceptance_trigger_fires_one_tick_above_prior_5min_high(calendar, settings):
    from decimal import Decimal

    eng = _engine(calendar, settings)
    eng.on_bar(_bar("10:00", 499.0, 500.00, 496.0, 497.5))  # price dips into the zone; bar high ₹500.00
    eng.on_bar(_bar("10:05", 497.5, 500.05, 497.0, 499.8))  # next bar trades one tick above
    assert eng.signal is not None and eng.signal.valid
    assert eng.signal.prior_bar.high == 500.00
    assert eng.signal.trigger_price == Decimal("500.05")


def test_acceptance_no_trigger_when_price_drops_through_zone(calendar, settings):
    eng = _engine(calendar, settings)
    eng.on_bar(_bar("10:00", 499.0, 499.2, 496.0, 496.5))  # into the zone
    eng.on_bar(_bar("10:05", 496.5, 496.6, 493.0, 493.2))  # straight down, no rebound bar
    eng.on_bar(_bar("10:10", 493.2, 493.3, 488.0, 488.4))
    assert eng.signal is None
    assert eng.zones[0].broken
    assert any(e.kind == "zone_broken" for e in eng.events)


# ------------------------------------------------------------------ 1. position size (Stage 3)
def test_acceptance_position_size(settings):
    from decimal import Decimal as D

    from nseswing.core.money import format_inr
    from nseswing.risk import RiskCalculator

    p = RiskCalculator(settings).plan("ACC", D("500.00"), D("487.50"))
    assert p.equity == D("1000000") and p.risk_pct == D("0.5")
    assert p.risk_per_share == D("12.50")
    assert p.max_loss == D("5000.00") and format_inr(p.max_loss, 0) == "₹5,000"
    assert p.shares == 400
    assert p.position_value == D("200000.00") and format_inr(p.position_value, 0) == "₹2,00,000"
    assert p.position_pct == D("20.00")
    assert p.stop_pct == D("2.50")


# ------------------------------------------------------------------ 2. R targets (Stage 4.1)
def test_acceptance_r_targets_gross(settings):
    from decimal import Decimal as D

    from nseswing.risk import RiskCalculator

    p = RiskCalculator(settings).plan("ACC", D("500.00"), D("487.50"))
    assert p.r == D("12.50")  # 1R
    assert p.targets["3R"] == D("537.50")
    assert p.targets["5R"] == D("562.50")
    # net of costs: the net target is above the gross one and the gross 3R nets less than 3R
    assert p.net_targets["3R"] > p.targets["3R"] and p.net_r_at_target["3R"] < D("3")


# ------------------------------------------------------------------ 5. tick rounding before sizing
def test_acceptance_stop_rounded_to_tick_before_shares(settings):
    from decimal import Decimal as D

    from nseswing.risk import RiskCalculator

    # ₹490.03 is not a valid price for a ₹500 stock (tick ₹0.05): it becomes ₹490.00 first.
    p = RiskCalculator(settings).plan("ACC", D("500.00"), D("490.03"))
    assert p.stop == D("490.00")
    assert p.risk_per_share == D("10.00")
    assert p.shares == 500  # unrounded risk ₹9.97 would have given 501 shares


# ------------------------------------------------------------------ 6. overnight stop for delivery positions
def _rig(calendar, **overrides):
    from decimal import Decimal as D

    from nseswing.alerts import AlertBus
    from nseswing.config import load_settings
    from nseswing.orders.base import OrderManager
    from nseswing.orders.paper import PaperBroker
    from nseswing.risk import RiskCalculator
    from nseswing.trademanager import TradeManager

    s = load_settings(overrides=overrides) if overrides else load_settings()
    alerts = AlertBus(s, console=False, file=None)
    broker = PaperBroker(s)
    tm = TradeManager(s, OrderManager(s, broker, alerts), alerts, calendar)
    plan = RiskCalculator(s).plan("ACC", D("500.00"), D("487.50"), tick=D("0.05"))
    tm.enter(plan, pd.Timestamp("2026-11-03 10:05", tz="Asia/Kolkata"), D("500.00"), cas_stock=False)
    return tm, broker, alerts


def test_acceptance_overnight_cnc_position_has_active_gtt(calendar):
    from nseswing.orders.base import OrderType

    tm, broker, _ = _rig(calendar)
    after_close = pd.Timestamp("2026-11-03 15:30", tz="Asia/Kolkata")
    tm.end_of_continuous_session("ACC", after_close)
    pos = tm.positions["ACC"]
    assert pos.plan.product == "CNC"
    assert pos.stop_kind == "GTT" and pos.stop_order.is_open and pos.stop_order.order_type == OrderType.GTT
    assert [o.order_type for o in broker.open_orders("ACC")] == [OrderType.GTT]  # the DAY SL expired
    assert tm.overnight_compliance(after_close + pd.Timedelta(minutes=1)) == {"ACC": "GTT stop active"}


def test_acceptance_overnight_flags_replace_sl_next_morning(calendar):
    from nseswing.intraday.bars import Bar
    from nseswing.orders.base import OrderType
    from nseswing.trademanager import REPLACE_FLAG

    tm, broker, alerts = _rig(calendar, trade_management={"overnight_stop": {"method": "replace_next_morning"}})
    after_close = pd.Timestamp("2026-11-03 15:30", tz="Asia/Kolkata")
    tm.end_of_continuous_session("ACC", after_close)
    pos = tm.positions["ACC"]
    assert REPLACE_FLAG in pos.flags and "REPLACE_SL" in alerts.kinds()
    assert broker.open_orders("ACC") == []
    assert tm.overnight_compliance(after_close).get("ACC", "").startswith("flagged: re-place SL")
    # next morning at 09:15 the SL is re-placed and the flag cleared
    open_ts = pd.Timestamp("2026-11-04 09:15", tz="Asia/Kolkata")
    tm.start_of_session("ACC", calendar.session(open_ts.date()), Bar(open_ts, open_ts + pd.Timedelta(minutes=1), 502, 503, 501, 502.5, 1000))
    assert REPLACE_FLAG not in pos.flags
    assert [o.order_type for o in broker.open_orders("ACC")] == [OrderType.SL_M]
