from decimal import Decimal

import pandas as pd
import pytest

from nseswing.config import load_settings
from nseswing.core.timeutil import IST
from nseswing.costs import ChargeModel
from nseswing.data.reference import CircuitLimits
from nseswing.intraday.bars import Bar
from nseswing.intraday.trigger import TriggerSignal
from nseswing.levels import Level, Zone
from nseswing.risk import InvalidPlan, RiskCalculator

D = Decimal


@pytest.fixture()
def rc(settings):
    return RiskCalculator(settings)


def test_wide_stop_warns_but_does_not_block(rc):
    p = rc.plan("X", D("500"), D("480"))  # 4% stop
    assert p.shares == 250 and any("wider than" in w for w in p.warnings)


def test_tight_stop_is_information_and_cash_caps_size():
    no_cap = RiskCalculator(load_settings(overrides={"risk": {"max_position_pct": None}}))
    p = no_cap.plan("X", D("500"), D("498.50"))  # 0.3% stop → 3,333 shares wanted = ₹16.7 L
    assert p.shares == 2000  # ₹10,00,000 of cash / ₹500
    assert any("capped by available cash" in w for w in p.warnings)
    assert any("tighter than" in i for i in p.info)


def test_position_capped_at_max_position_pct(rc, settings):
    assert settings.risk.max_position_pct == D("25")
    p = rc.plan("X", D("500"), D("495"))  # 1% stop → 1,000 shares = ₹5,00,000 (50%) wanted
    assert p.shares == 500  # 25% of ₹10,00,000 / ₹500
    assert p.position_value == D("250000.00") and p.position_pct == D("25.00")
    assert p.planned_loss == D("2500.00") and p.planned_loss < p.max_loss  # risk falls below the 0.5% budget
    assert any("capped at 25% of equity: 1000 → 500 shares" in w for w in p.warnings)
    # a plan already under the cap is untouched (the acceptance example is 20%)
    assert rc.plan("X", D("500.00"), D("487.50")).shares == 400


def test_no_position_when_risk_exceeds_budget():
    s = load_settings(overrides={"account": {"equity_inr": 10000}})
    p = RiskCalculator(s).plan("X", D("500"), D("400"))
    assert p.shares == 0 and any("no position" in w for w in p.warnings)


def test_stop_must_be_below_entry(rc):
    with pytest.raises(InvalidPlan):
        rc.plan("X", D("500"), D("500.02"))  # rounds down to 500.00 = entry


def test_entry_rounds_up_targets_round_down(rc):
    p = rc.plan("X", D("500.02"), D("487.50"))
    assert p.entry == D("500.05") and p.risk_per_share == D("12.55")
    assert p.targets["3R"] == D("537.70")  # 500.05 + 37.65 = 537.70 (already on tick)


def test_choose_stop_methods(rc, settings):
    t0 = pd.Timestamp("2026-11-03 10:05", tz=IST)
    prior = Bar(t0 - pd.Timedelta(minutes=1), t0, 496.0, 497.0, 495.12, 496.8, 1)
    trig = Bar(t0, t0 + pd.Timedelta(minutes=1), 496.8, 497.4, 496.33, 497.2, 1)
    zone = Zone("X", D("494"), D("498"), [Level("avwap", 496.0, "AVWAP")], "low")
    sig = TriggerSignal("X", zone, t0, D("497.05"), D("497.05"), trig, prior, day_low=494.57, dip_low=494.57, valid=True)
    assert rc.choose_stop(sig, D("0.05"), "day_low")[0] == D("494.55")
    assert rc.choose_stop(sig, D("0.05"), "trigger_bar_low")[0] == D("496.30")
    assert rc.choose_stop(sig, D("0.05"), "prior_bar_low")[0] == D("495.10")
    plan = rc.plan_from_signal(sig, D("0.05"))
    assert plan.stop == D("494.55") and plan.stop_method == "day_low" and plan.trigger_price == D("497.05")


def test_lower_circuit_worst_case(rc):
    circ = CircuitLimits(D("450.00"), D("550.00"), 10.0, "band")
    p = rc.plan("X", D("500"), D("487.50"), circuit=circ)
    assert p.lower_circuit_loss == D("20000.00")
    assert any("lower circuit ₹450.00 would lose ₹20,000" in w for w in p.warnings)


def test_charges_components(settings):
    cm = ChargeModel(settings)
    buy = cm.leg("BUY", 400, D("500"))
    assert (buy.brokerage, buy.stt, buy.stamp_duty, buy.dp) == (D("20.00"), D("200.00"), D("30.00"), D("0.00"))
    assert buy.gst == D("4.74") and buy.total == D("261.08")
    sell = cm.leg("SELL", 400, D("537.50"))
    assert sell.dp == D("20.00") and sell.stamp_duty == D("0.00") and sell.stt == D("215.00")
    small = cm.leg("BUY", 1, D("100"))  # 2.5% cap beats ₹20 on a ₹100 order
    assert small.brokerage == D("2.50")
    mis = cm.leg("BUY", 400, D("500"), product="MIS")
    assert mis.stt == D("0.00") and mis.stamp_duty == D("6.00")


def test_net_target_solver_is_tight(settings):
    cm = ChargeModel(settings)
    price = cm.net_target_price(D("500"), D("12.50"), D("3"), 400, D("0.05"))
    assert cm.net_pnl(400, D("500"), price) >= D("15000")
    assert cm.net_pnl(400, D("500"), price - D("0.05")) < D("15000")


def test_draft_plan_from_zone(rc):
    zone = Zone("X", D("466.00"), D("471.00"), [Level("avwap", 468.0, "AVWAP"), Level("ema21", 469.0, "EMA21")], "medium")
    p = rc.draft_plan("X", zone, D("0.05"))
    assert p.is_draft and p.entry == D("471.05") and p.stop == D("464.80")
    assert p.info[0].startswith("DRAFT")
