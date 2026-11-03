from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from nseswing.data.base import Quote
from nseswing.data.reference import ReferenceData, SecurityInfo
from nseswing.levels import Level, ZoneMemory, _cluster, best_zone, build_level_map, retest_status
from nseswing.preopen import classify, expected_open, preopen_list
from nseswing.screener import Screener
from nseswing.synthetic import make_universe

D = Decimal


@pytest.fixture(scope="module")
def universe(calendar):
    return make_universe(calendar, date(2026, 10, 30))


@pytest.fixture(scope="module")
def screen(settings, universe):
    data, profiles = universe
    ref = ReferenceData({s: SecurityInfo(s, s, industry=p.industry, band_pct=20.0, band_source="sec_list") for s, p in profiles.items()})
    return Screener(settings, ref).run(data)


def test_priority_recipe(settings):
    levels = [
        Level("avwap", 498.0, "AVWAP"),
        Level("ema21", 497.0, "EMA21"),
        Level("prior_high", 500.0, "prior high"),
        Level("ema9", 471.0, "EMA9"),
        Level("avwap", 470.0, "AVWAP manual"),
        Level("swing_low", 440.0, "swing low"),
        Level("ema9", 455.0, "EMA9"),
        Level("ema50", 454.0, "EMA50"),
    ]
    zones = _cluster("X", levels, settings, D("0.05"))
    by_top = {float(z.top): z.priority for z in zones}
    assert [z.priority for z in zones] == ["high", "medium", "low", "low"]
    assert zones[0].kinds == {"avwap", "ema21", "prior_high"}
    assert zones[2].kinds == {"ema9", "ema50"}  # two EMAs = one level type → low
    # zone edges are valid ticks and padded around the members
    assert zones[0].bottom == D("496.25") and zones[0].top == D("500.75")
    assert len(by_top) == 4


def test_level_map_on_leader(settings, universe, screen):
    data, _ = universe
    row = screen.get("ALPHA.SYN")
    lm = build_level_map("ALPHA.SYN", data["ALPHA.SYN"], settings, row.consolidation)
    assert lm.default_avwap is not None and lm.default_avwap.anchor_date == row.consolidation.high_date
    z = best_zone(lm.zones)
    assert z.priority == "medium" and {"avwap", "ema21"} <= z.kinds
    assert all(float(z.bottom) <= lm.last_close * 1.01 for z in lm.zones)
    # swing lows of the base are marked as support
    assert any(lv.kind == "swing_low" for lv in lm.levels)


def test_manual_anchor_adds_avwap(settings, universe, screen):
    data, _ = universe
    row = screen.get("ALPHA.SYN")
    lm = build_level_map("ALPHA.SYN", data["ALPHA.SYN"], settings, row.consolidation, manual_anchors=[{"date": "2026-10-14", "label": "second low"}])
    assert [a.source for a in lm.avwaps] == ["default", "manual"]
    assert lm.avwaps[1].anchor_date == pd.Timestamp("2026-10-14")


def _frame(closes, lows=None):
    idx = pd.bdate_range("2026-01-05", periods=len(closes))
    lows = lows or [c - 1 for c in closes]
    return pd.DataFrame({"open": closes, "high": [c + 1 for c in closes], "low": lows, "close": closes, "volume": 1000.0}, index=idx, dtype=float)


def test_first_retest_states():
    closes = [95, 98, 102, 104, 106]
    df = _frame(closes)
    assert retest_status(100.0, df, 0, 0.3)[0] == "pending"
    df = _frame(closes + [101], lows=[94, 97, 101, 103, 105, 100.2])
    status, brk = retest_status(100.0, df, 0, 0.3)
    assert status == "in_progress" and brk == df.index[2]
    df = _frame(closes + [101, 103], lows=[94, 97, 101, 103, 105, 100.2, 102])
    assert retest_status(100.0, df, 0, 0.3)[0] is None  # already retested
    df = _frame(closes + [99])
    assert retest_status(100.0, df, 0, 0.3)[0] is None  # lost the level


def test_zone_memory_marks_and_forgets(tmp_path, settings, universe, screen):
    data, _ = universe
    df = data["ALPHA.SYN"]
    row = screen.get("ALPHA.SYN")
    lm = build_level_map("ALPHA.SYN", df, settings, row.consolidation)
    z = best_zone(lm.zones)
    mem = ZoneMemory(tmp_path / "broken.json")
    mem.mark_broken("ALPHA.SYN", z.bottom, z.top, df.index[-1].date(), "fell through")
    lm2 = build_level_map("ALPHA.SYN", df, settings, row.consolidation, memory=ZoneMemory(tmp_path / "broken.json"))
    hit = next(x for x in lm2.zones if x.zone_id == z.zone_id)
    assert hit.broken and best_zone(lm2.zones).zone_id != z.zone_id
    # a later close above the zone top clears the record
    later = pd.concat([df, pd.DataFrame({"open": [500.0], "high": [505.0], "low": [495.0], "close": [503.0], "volume": [1e6]}, index=[pd.Timestamp("2026-11-02")])])
    mem3 = ZoneMemory(tmp_path / "broken.json")
    build_level_map("ALPHA.SYN", later, settings, row.consolidation, memory=mem3)
    assert mem3.records == []


def test_preopen_classification(settings, universe, screen):
    data, _ = universe
    row = screen.get("ALPHA.SYN")
    lm = build_level_map("ALPHA.SYN", data["ALPHA.SYN"], settings, row.consolidation)
    z = best_zone(lm.zones)
    top = float(z.top)
    assert classify(lm, top * 1.02, settings)[2] == "near"
    assert classify(lm, float(z.mid), settings)[2] == "inside"
    assert classify(lm, top * 1.06, settings)[2] == "far"
    zone, _, status, broken = classify(lm, float(lm.zones[-1].bottom) * 0.98, settings)
    assert zone is None and status == "opened_below" and len(broken) == len(lm.zones)


def test_preopen_list_uses_quote_and_sorts(settings, universe, screen):
    data, _ = universe
    maps = {s: build_level_map(s, data[s], settings, screen.get(s).consolidation) for s in ("ALPHA.SYN", "BRAVO.SYN", "CHARLIE.SYN")}
    gap_up = {s: Quote(s, ltp=float(best_zone(m.zones).top) * 1.015, prev_close=m.last_close) for s, m in maps.items()}
    out = preopen_list(screen, maps, settings, quotes=gap_up)
    assert {c.symbol for c in out} == set(maps)
    assert all(c.status == "near" and c.source == "upstox pre-open quote" for c in out)
    eo, src = expected_open("X", 100.0, settings)
    assert (eo, src) == (100.0, "previous close (no pre-open data)")
