from datetime import date

import pytest

from nseswing.config import load_settings
from nseswing.data.reference import ReferenceData, SecurityInfo
from nseswing.screener import Screener, momentum_check
from nseswing.synthetic import make_universe

AS_OF = date(2026, 10, 30)


@pytest.fixture(scope="module")
def universe(calendar):
    return make_universe(calendar, AS_OF)


def _ref(profiles, **extra):
    secs = {sym: SecurityInfo(sym, sym, industry=p.industry, band_pct=20.0, band_source="sec_list") for sym, p in profiles.items()}
    for sym, kw in extra.items():
        for k, v in kw.items():
            setattr(secs[sym], k, v)
    return ReferenceData(secs)


@pytest.fixture(scope="module")
def result(settings, universe):
    data, profiles = universe
    return Screener(settings, _ref(profiles)).run(data)


def test_archetypes_pass_and_fail_for_the_right_reason(result):
    get = result.get
    for sym in ("ALPHA.SYN", "BRAVO.SYN", "CHARLIE.SYN"):
        assert get(sym).all_checks_pass, sym
    assert not get("DELTA.SYN").weekly.passed and "no pullback" in get("DELTA.SYN").weekly.reason
    assert not get("HOTEL.SYN").liquidity.passed and not get("HOTEL.SYN").on_watchlist
    assert not get("FOXTROT.SYN").higher_lows.passed and "lower low" in get("FOXTROT.SYN").higher_lows.reason
    assert not get("INDIA.SYN").contraction.passed
    assert not get("GOLF.SYN").momentum.passed
    assert get("ECHO.SYN").disqualified.passed and not get("ECHO.SYN").all_checks_pass


def test_hot_group_and_rank_order(result):
    assert "Industry: Capital Goods" in result.hot_groups
    wl = [r.symbol for r in result.watchlist]
    # all-pass names first (all in the hot group), fastest 1-month riser first
    assert set(wl[:3]) == {"ALPHA.SYN", "BRAVO.SYN", "CHARLIE.SYN"}
    speeds = [result.get(s).fastest("ret_m1") for s in wl[:3]]
    assert speeds == sorted(speeds, reverse=True)
    assert all(result.get(s).hot_groups for s in wl[:3])
    first_fail = next(i for i, s in enumerate(wl) if not result.get(s).all_checks_pass)
    assert all(not result.get(s).all_checks_pass for s in wl[first_fail:])


def test_every_check_has_a_reason(result):
    for row in result.watchlist:
        for chk in row.checks():
            assert chk.reason, (row.symbol, chk.name)
    tbl = result.table()
    assert {"symbol", "tier", "weekly", "7a_higher_lows", "7b_contraction", "7c_ema", "8_disqualified", "flags", "reasons"} <= set(tbl.columns)


def test_momentum_both_conditions_scope(settings):
    returns = {"m1": 35.0, "m3": 12.0, "m6": 20.0}
    top = {"m1": False, "m3": True, "m6": False}
    chk, both = momentum_check(returns, top, settings)
    assert chk.passed and not both  # absolute in m1, relative in m3: not the same window
    any_window = load_settings(overrides={"screener": {"momentum": {"both_conditions_scope": "any_window"}}})
    assert momentum_check(returns, top, any_window)[1]
    chk, both = momentum_check({"m1": 10.0, "m3": 5.0, "m6": 2.0}, {"m1": False, "m3": False, "m6": False}, settings)
    assert not chk.passed


def test_top_rank_cutoff_uses_setting(universe):
    data, profiles = universe
    s20 = load_settings(overrides={"screener": {"momentum": {"top_rank_pct": 20}}})
    res = Screener(s20, _ref(profiles)).run(data)
    n = len([r for r in res.rows if "m1" in r.top])
    assert sum(r.top.get("m1", False) for r in res.rows) == -(-20 * n // 100)


def test_surveillance_flags_and_optional_exclusion(universe):
    data, profiles = universe
    ref = _ref(profiles, **{"ALPHA.SYN": {"asm": "ST-ASM Stage I"}})
    flagged = Screener(load_settings(), ref).run(data).get("ALPHA.SYN")
    assert "ST-ASM Stage I" in flagged.flags and flagged.on_watchlist
    excl = Screener(load_settings(overrides={"surveillance": {"exclude_asm": True}}), ref).run(data).get("ALPHA.SYN")
    assert excl.excluded_reason and not excl.on_watchlist


def test_short_history_is_skipped_with_reason(settings, universe):
    data, profiles = universe
    short = {"ALPHA.SYN": data["ALPHA.SYN"].iloc[-60:]}
    row = Screener(settings, _ref(profiles)).run(short).get("ALPHA.SYN")
    assert row.skipped_reason and "daily bars" in row.skipped_reason
