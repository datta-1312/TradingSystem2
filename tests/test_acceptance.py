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
