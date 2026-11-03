from datetime import date, datetime, time
from decimal import Decimal

import pytest
from pydantic import ValidationError

from nseswing.config import load_settings
from nseswing.core.money import format_inr, format_inr_short, group_indian
from nseswing.core.ticks import is_on_tick, round_to_tick, to_decimal
from nseswing.core.timeutil import IST

D = Decimal


# ------------------------------------------------------------------ config
def test_settings_load_and_forbid_unknown_keys():
    s = load_settings()
    assert s.account.equity_inr == D("1000000")
    assert s.execution.mode == "paper"
    assert s.live_orders_allowed is False
    with pytest.raises(ValidationError):
        load_settings(overrides={"risk": {"stop_methd": "day_low"}})


def test_live_needs_all_switches():
    s = load_settings(overrides={"execution": {"mode": "live"}})
    assert not s.live_orders_allowed
    s = load_settings(overrides={"execution": {"mode": "live", "live": {"enabled": True}}})
    assert not s.live_orders_allowed
    s = load_settings(
        overrides={"execution": {"mode": "live", "live": {"enabled": True, "confirm_live_trading": True}}}
    )
    assert s.live_orders_allowed


def test_partials_must_leave_shares_to_trail():
    with pytest.raises(ValidationError):
        load_settings(overrides={"trade_management": {"partials": [{"at_r": 3, "fraction": 0.5}, {"at_r": 5, "fraction": 0.5}]}})


# ------------------------------------------------------------------ ticks
@pytest.mark.parametrize(
    "price,tick",
    [
        (D("100"), D("0.01")),
        (D("249.99"), D("0.01")),
        (D("250"), D("0.05")),
        (D("500"), D("0.05")),
        (D("1000"), D("0.05")),
        (D("1000.10"), D("0.10")),
        (D("4999"), D("0.10")),
        (D("7500"), D("0.50")),
        (D("15000"), D("1.00")),
        (D("25000"), D("5.00")),
    ],
)
def test_tick_table_slabs(ticks, price, tick):
    assert ticks.tick_for(price) == tick


def test_rounding_directions(ticks):
    t = D("0.05")
    assert round_to_tick(D("487.53"), t, "down") == D("487.50")
    assert round_to_tick(D("487.53"), t, "up") == D("487.55")
    assert round_to_tick(D("487.53"), t, "nearest") == D("487.55")
    assert round_to_tick(D("487.52"), t, "nearest") == D("487.50")
    # float noise must not push a valid price to the next tick
    assert round_to_tick(500.0000000001, t, "up") == D("500.00")
    assert round_to_tick(499.9999999999, t, "down") == D("500.00")
    assert is_on_tick(D("487.50"), t) and not is_on_tick(D("487.52"), t)
    assert to_decimal(0.1) == D("0.1")


def test_tick_uses_reference_price_slab(ticks):
    # a stock whose slab was set by a ₹1,005 close keeps the ₹0.10 tick at ₹998
    assert ticks.round(D("998.07"), "down", reference_price=D("1005")) == D("998.00")
    assert ticks.round(D("998.07"), "down") == D("998.05")


def test_instrument_tick_unit_auto(ticks):
    assert ticks.normalise_instrument_tick(0.05, D("500"), "auto") == D("0.05")
    assert ticks.normalise_instrument_tick(5.0, D("500"), "auto") == D("0.05")
    assert ticks.normalise_instrument_tick(5.0, D("25000"), "auto") == D("5.00")
    assert ticks.normalise_instrument_tick(1.0, D("200"), "auto") == D("0.01")


# ------------------------------------------------------------------ money
@pytest.mark.parametrize(
    "amount,decimals,text",
    [
        (1000000, 0, "₹10,00,000"),
        (200000, 0, "₹2,00,000"),
        (5000, 0, "₹5,000"),
        (12.5, 2, "₹12.50"),
        (123456789.456, 2, "₹12,34,56,789.46"),
        (-5000, 2, "-₹5,000.00"),
        (999, 0, "₹999"),
    ],
)
def test_format_inr(amount, decimals, text):
    assert format_inr(amount, decimals) == text


def test_group_and_short():
    assert group_indian("1234567") == "12,34,567"
    assert format_inr_short(1000000) == "₹10.00 L"
    assert format_inr_short(12500000) == "₹1.25 Cr"
    assert format_inr_short(5000) == "₹5,000"


# ------------------------------------------------------------------ calendar
def test_holidays_weekends_and_special_sessions(calendar):
    assert not calendar.is_trading_day(date(2026, 1, 26))  # Republic Day
    assert not calendar.is_trading_day(date(2026, 11, 7))  # Saturday
    assert calendar.is_trading_day(date(2026, 2, 1))  # Budget day, Sunday special session
    assert calendar.is_trading_day(date(2026, 11, 8))  # Muhurat, Sunday
    assert calendar.is_trading_day(date(2026, 11, 3))
    muhurat = calendar.session(date(2026, 11, 8))
    assert muhurat.special and muhurat.market_open.time() == time(14, 20)


def test_trading_day_arithmetic(calendar):
    # Friday 6 Nov 2026 → next session is the Muhurat Sunday, then Monday 9 Nov
    assert calendar.next_trading_day(date(2026, 11, 6)) == date(2026, 11, 8)
    assert calendar.next_trading_day(date(2026, 11, 8)) == date(2026, 11, 9)
    # Monday 9 Nov → Wednesday 11 Nov (10 Nov Balipratipada)
    assert calendar.next_trading_day(date(2026, 11, 9)) == date(2026, 11, 11)
    assert calendar.previous_trading_day(date(2026, 1, 27)) == date(2026, 1, 23)
    assert calendar.settlement_date(date(2026, 3, 2)) == date(2026, 3, 4)  # T+1 skips Holi
    days = calendar.trading_days(date(2026, 10, 1), date(2026, 10, 31))
    assert date(2026, 10, 2) not in days and date(2026, 10, 20) not in days
    assert len(days) == 20


def test_cas_session_profile(calendar):
    d = date(2026, 11, 3)
    sess = calendar.session(d)
    assert calendar.is_cas_stock(d, is_fno=True)
    assert not calendar.is_cas_stock(d, is_fno=False)
    assert sess.continuous_end(cas_stock=True).time() == time(15, 15)
    assert sess.continuous_end(cas_stock=False).time() == time(15, 30)
    ts = datetime(2026, 11, 3, 15, 20, tzinfo=IST)
    assert sess.phase(ts, cas_stock=True) == "closing_auction"
    assert sess.phase(ts, cas_stock=False) == "continuous"
    assert sess.phase(datetime(2026, 11, 3, 9, 5, tzinfo=IST), cas_stock=False) == "preopen"
    # before CAS went live, F&O stocks traded until 15:30
    old = calendar.session(date(2026, 7, 31))
    assert not calendar.is_cas_stock(date(2026, 7, 31), is_fno=True)
    assert old.continuous_end(cas_stock=True).time() == time(15, 30)


def test_missing_year_warns_or_fails(settings, caplog):
    from nseswing.core.calendar import CalendarYearMissing, NSECalendar

    cal = NSECalendar.from_settings(settings)
    assert cal.is_trading_day(date(2027, 1, 4))  # warns, weekday treated as session
    strict = NSECalendar.from_settings(load_settings(overrides={"calendar": {"fail_if_year_missing": True}}))
    with pytest.raises(CalendarYearMissing):
        strict.is_trading_day(date(2027, 1, 4))


def test_numpy_scalars_convert_cleanly():
    import numpy as np

    from nseswing.core.money import format_inr

    assert to_decimal(np.float64(494.25750000000005)) == D("494.2575")
    assert to_decimal(np.int64(5)) == D("5")
    assert format_inr(np.float64(200000.0), 0) == "₹2,00,000"
