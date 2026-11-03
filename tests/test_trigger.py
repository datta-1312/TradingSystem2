from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from nseswing.config import load_settings
from nseswing.core.timeutil import IST
from nseswing.intraday.bars import Bar
from nseswing.intraday.trigger import State, TriggerEngine
from nseswing.levels import Level, Zone, ZoneMemory

D = Decimal
DAY = date(2026, 11, 3)


def bar(hhmm: str, o, h, l, c, minutes=5):
    start = pd.Timestamp(f"{DAY} {hhmm}", tz=IST)
    return Bar(start, start + pd.Timedelta(minutes=minutes), o, h, l, c, 10_000.0)


def zones():
    return [
        Zone("XYZ", D("494.00"), D("499.00"), [Level("avwap", 497.0, "AVWAP"), Level("ema21", 496.0, "EMA21")], "medium"),
        Zone("XYZ", D("470.00"), D("474.00"), [Level("ema50", 472.0, "EMA50")], "low"),
    ]


def engine(calendar, settings=None, **kw):
    s = settings or load_settings()
    return TriggerEngine("XYZ", zones(), D("0.05"), s, calendar.session(DAY), stage1_pass=kw.pop("stage1_pass", True), **kw)


def test_trigger_fires_one_tick_above_previous_bar_high(calendar):
    eng = engine(calendar)
    eng.on_bar(bar("09:15", 503.0, 503.5, 499.6, 500.0))  # approach
    assert eng.state == State.APPROACH
    eng.on_bar(bar("09:20", 500.0, 500.00, 496.5, 497.0))  # red bar dips into the zone; high ₹500.00
    assert eng.state == State.ARMED and eng.trigger_level() == D("500.05")
    events = eng.on_bar(bar("09:25", 497.0, 500.05, 496.8, 499.9))  # trades one tick above
    assert [e.kind for e in events] == ["trigger"]
    sig = eng.signal
    assert sig.valid and sig.trigger_price == D("500.05") and sig.prior_bar.high == 500.0
    assert sig.day_low == 496.5 and sig.dip_low == 496.5


def test_no_trigger_when_bar_only_touches_previous_high(calendar):
    eng = engine(calendar)
    eng.on_bar(bar("09:20", 500.0, 500.00, 496.5, 497.0))
    eng.on_bar(bar("09:25", 497.0, 500.00, 496.8, 499.9))  # equals the high, not above
    assert eng.signal is None and eng.state == State.ARMED


def test_falling_through_zone_gives_no_trigger_and_marks_it_broken(calendar, tmp_path):
    mem = ZoneMemory(tmp_path / "broken.json")
    eng = engine(calendar, memory=mem)
    eng.on_bar(bar("09:15", 501.0, 501.2, 498.0, 498.5))  # dips in
    eng.on_bar(bar("09:20", 498.5, 498.6, 495.0, 495.5))  # lower high, still in zone
    events = eng.on_bar(bar("09:25", 495.5, 495.6, 489.0, 489.5))  # closes below zone bottom
    kinds = [e.kind for e in events]
    assert "zone_broken" in kinds and "trigger" not in kinds and eng.signal is None
    assert eng.zones[0].broken and mem.records[0]["top"] == "499.00"
    # keeps waiting at the next zone
    assert eng.zone.top == D("474.00") and eng.state == State.WAITING


def test_green_dip_bar_can_trigger_itself_but_red_cannot(calendar):
    eng = engine(calendar)
    eng.on_bar(bar("09:15", 502.0, 502.4, 500.5, 501.0))
    eng.on_bar(bar("09:20", 501.0, 503.0, 497.5, 502.8))  # green: low (in zone) then above ₹502.40
    assert eng.signal is not None and eng.signal.trigger_price == D("502.45")
    eng2 = engine(calendar)
    eng2.on_bar(bar("09:15", 502.0, 502.4, 500.5, 501.0))
    eng2.on_bar(bar("09:20", 502.0, 503.0, 497.5, 497.8))  # red: high came first
    assert eng2.signal is None and eng2.state == State.ARMED


def test_invalid_when_stage1_fails_or_far_above_zone(calendar):
    eng = engine(calendar, stage1_pass=False)
    eng.on_bar(bar("09:20", 500.0, 500.0, 496.5, 497.0))
    eng.on_bar(bar("09:25", 497.0, 501.0, 496.8, 500.5))
    assert eng.signal is None and eng.events[-1].kind == "invalid_trigger" and eng.state == State.DONE
    eng2 = engine(calendar)
    eng2.on_bar(bar("09:20", 506.0, 508.0, 498.5, 507.0))  # dips in with a high far above
    eng2.on_bar(bar("09:25", 507.0, 508.5, 505.0, 508.0))
    assert eng2.signal is None and "not at support" in eng2.events[-1].message


def test_cas_stock_stops_at_1515(calendar):
    eng = engine(calendar, cas_stock=True)
    eng.on_bar(bar("15:05", 500.0, 500.0, 496.5, 497.0))
    events = eng.on_bar(bar("15:15", 497.0, 501.0, 496.8, 500.5))  # closing auction: ignored
    assert events == [] and eng.signal is None
    non_cas = engine(calendar, cas_stock=False)
    non_cas.on_bar(bar("15:05", 500.0, 500.0, 496.5, 497.0))
    non_cas.on_bar(bar("15:15", 497.0, 501.0, 496.8, 500.5))
    assert non_cas.signal is not None


def test_intrabar_ticks(calendar):
    eng = engine(calendar)
    eng.on_bar(bar("09:15", 501.0, 501.5, 499.2, 499.5))
    t = lambda hh, mm, ss: datetime(2026, 11, 3, hh, mm, ss, tzinfo=IST)  # noqa: E731
    eng.on_price(t(9, 20, 5), 498.4)  # trades into the zone
    assert eng.state == State.ARMED
    eng.on_price(t(9, 21, 0), 501.55)  # first trade above ₹501.50 + 1 tick
    assert eng.signal is not None and eng.signal.fill_reference == D("501.55")
