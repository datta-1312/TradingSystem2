from datetime import date

import numpy as np
import pandas as pd
import pytest

from nseswing.config import load_settings
from nseswing.corpactions import (
    CorporateAction,
    adjust_intraday,
    adjust_ohlcv,
    parse_nse_actions_json,
    parse_nse_subject,
)
from nseswing.indicators import (
    atr,
    avwap,
    avwap_state,
    ema,
    find_gaps,
    resample_weekly,
    slope_pct_per_day,
    swing_points,
)


def frame(rows, start="2026-01-05"):
    """rows: list of (open, high, low, close, volume); index = consecutive business days."""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"], dtype=float)


def flat_frame(closes, vol=1000.0, spread=1.0):
    rows = [(c, c + spread, c - spread, c, vol) for c in closes]
    return frame(rows)


# ------------------------------------------------------------------ indicators
def test_ema_matches_recursive_definition():
    s = pd.Series([10.0, 11.0, 12.0, 13.0])
    out = ema(s, 3)
    alpha = 2 / (3 + 1)
    expected = [10.0]
    for x in s.iloc[1:]:
        expected.append(alpha * x + (1 - alpha) * expected[-1])
    assert np.allclose(out.to_numpy(), expected)


def test_atr_and_slope():
    df = flat_frame([100.0] * 20, spread=1.0)
    assert atr(df, 14).iloc[-1] == pytest.approx(2.0)
    s = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    assert slope_pct_per_day(s, 5).iloc[-1] == pytest.approx(1.0)


def test_swing_lows_confirmed_and_provisional():
    lows = [10, 9, 8, 7, 8, 9, 10, 9, 8.5, 9, 10, 11]
    df = frame([(lo + 1, lo + 2, lo, lo + 1, 100) for lo in lows])
    swings = swing_points(df, 3, "low")
    assert [(s.pos, s.price) for s in swings] == [(3, 7.0), (8, 8.5)]
    assert all(s.confirmed for s in swings)
    # last bar dips: provisional swing only when asked for
    df2 = frame([(lo + 1, lo + 2, lo, lo + 1, 100) for lo in lows + [8.8, 9.5]])
    assert all(s.confirmed for s in swing_points(df2, 3, "low"))
    prov = swing_points(df2, 3, "low", include_provisional=True)
    assert not prov[-1].confirmed and prov[-1].price == 8.8


def test_gap_up_partial_fill_and_full_fill():
    rows = [
        (100, 101, 99, 100, 1000),
        (104, 106, 103, 105, 1000),  # gap up: 101 → 103
        (105, 107, 102, 106, 1000),  # partially fills the gap down to 102
        (106, 108, 104, 107, 1000),
    ]
    g = find_gaps(frame(rows), min_gap_pct=0.5, lookback=10)
    assert len(g) == 1 and g[0].direction == "up"
    assert (g[0].bottom, g[0].top, g[0].filled) == (101.0, 102.0, False)
    rows.append((104, 105, 100.5, 101, 1000))  # trades through 101 → filled
    g = find_gaps(frame(rows), min_gap_pct=0.5, lookback=10)
    assert g[0].filled


def test_avwap_from_anchor():
    rows = [(10, 11, 9, 10, 100), (20, 21, 19, 20, 100), (30, 31, 29, 30, 300)]
    v = avwap(frame(rows), anchor_pos=1)
    assert np.isnan(v.iloc[0])
    assert v.iloc[1] == pytest.approx(20.0)
    assert v.iloc[2] == pytest.approx((20 * 100 + 30 * 300) / 400)


def test_avwap_state_reclaim_then_pullback():
    idx = pd.bdate_range("2026-01-05", periods=6)
    vwap = pd.Series([100.0] * 6, index=idx)
    close = pd.Series([99, 98, 101, 103, 102, 101.5], index=idx, dtype=float)
    low = pd.Series([98, 97, 99, 102, 101, 100.8], index=idx, dtype=float)
    st = avwap_state(close, low, vwap, near_pct=1.5)
    assert st["state"] == "pullback" and st["reclaim_date"] == idx[2]
    close.iloc[-1] = 99.0
    assert avwap_state(close, low, vwap, near_pct=1.5)["state"] == "lost"


def test_weekly_resample_labels_last_session():
    idx = pd.DatetimeIndex(["2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02", "2026-04-06"])  # 3 Apr = Good Friday
    df = pd.DataFrame(
        {"open": [1, 2, 3, 4, 5], "high": [2, 3, 4, 9, 6], "low": [0.5, 1, 2, 3, 4], "close": [1.5, 2.5, 3.5, 4.5, 5.5], "volume": [10] * 5},
        index=idx,
        dtype=float,
    )
    wk = resample_weekly(df)
    assert list(wk.index) == [pd.Timestamp("2026-04-02"), pd.Timestamp("2026-04-06")]
    assert wk.iloc[0].to_dict() == {"open": 1.0, "high": 9.0, "low": 0.5, "close": 4.5, "volume": 40.0}


# ------------------------------------------------------------------ corporate actions: parsing
def test_parse_split_bonus_rights_dividend_demerger():
    d = date(2026, 5, 4)
    split = parse_nse_subject("ABC", d, "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share")
    assert split[0].kind == "split" and split[0].factor(100) == pytest.approx(0.2)
    re1 = parse_nse_subject("ABC", d, "Face Value Split (Sub-Division) - From Rs 2/- Per Share To Re 1/- Per Share")
    assert re1[0].factor(100) == pytest.approx(0.5)
    cons = parse_nse_subject("ABC", d, "Consolidation of shares From Rs 1/- To Rs 10/-")
    assert cons[0].kind == "consolidation" and cons[0].factor(10) == pytest.approx(10.0)
    bonus = parse_nse_subject("ABC", d, "Bonus 1:1")
    assert bonus[0].factor(None) == pytest.approx(0.5)
    bonus32 = parse_nse_subject("ABC", d, "Bonus 3:2")
    assert bonus32[0].factor(None) == pytest.approx(2 / 5)
    rights = parse_nse_subject("ABC", d, "Rights 1:5 @ Premium Rs 90/-", face_value=10)
    assert rights[0].amount == 100.0
    # TERP = (5*160 + 1*100)/6 = 150 → factor 150/160
    assert rights[0].factor(160.0) == pytest.approx(150 / 160)
    divs = parse_nse_subject("ABC", d, "Interim Dividend - Rs 5 Per Share / Special Dividend - Rs 2.50 Per Share")
    assert divs[0].kind == "dividend" and divs[0].amount == 7.5
    dem = parse_nse_subject("ABC", d, "Demerger")
    assert dem[0].kind == "demerger" and dem[0].factor(100) is None
    assert parse_nse_subject("ABC", d, "Annual General Meeting") == []


def test_parse_nse_json_payload():
    payload = [
        {"symbol": "ABC", "subject": "Bonus 1:1", "exDate": "15-Jun-2026", "faceVal": "10"},
        {"symbol": "XYZ", "subject": "Dividend - Rs 4 Per Share", "exDate": "-", "faceVal": "1"},
    ]
    acts = parse_nse_actions_json(payload)
    assert len(acts) == 1 and acts[0].ex_date == date(2026, 6, 15)


# ------------------------------------------------------------------ corporate actions: adjustment
def _bonus_series(adjusted_by_source: bool):
    """Price ~200 before a 1:1 bonus on day 6, ~100 after (unless the source already adjusted)."""
    before = [200, 202, 204, 203, 205, 206]
    after = [103.5, 104, 105, 104.5]
    if adjusted_by_source:
        before = [p / 2 for p in before]
    closes = before + after
    rows = []
    for i, c in enumerate(closes):
        o = c if i != len(before) else 103.0
        rows.append((o, c + 1, c - 1, c, 1000.0 if i < len(before) and not adjusted_by_source else 2000.0))
    return frame(rows, start="2026-06-08")


def test_bonus_applied_to_unadjusted_data(settings):
    df = _bonus_series(adjusted_by_source=False)
    ex = df.index[6].date()
    act = CorporateAction("ABC", ex, "bonus", "Bonus 1:1", ratio_new=1, ratio_held=1)
    res = adjust_ohlcv(df, [act], settings.corporate_actions)
    assert res.actions[0].decision == "applied"
    assert res.df["close"].iloc[5] == pytest.approx(103.0)
    assert res.df["volume"].iloc[0] == pytest.approx(2000.0)
    assert res.df["close"].iloc[7] == pytest.approx(df["close"].iloc[7])
    assert res.warnings == []


def test_bonus_skipped_when_source_already_adjusted(settings):
    df = _bonus_series(adjusted_by_source=True)
    act = CorporateAction("ABC", df.index[6].date(), "bonus", "Bonus 1:1", ratio_new=1, ratio_held=1)
    res = adjust_ohlcv(df, [act], settings.corporate_actions)
    assert res.actions[0].decision == "skipped_already_adjusted"
    pd.testing.assert_frame_equal(res.df, df)


def test_always_mode_double_adjusts_and_warns():
    s = load_settings(overrides={"corporate_actions": {"mode": "always"}})
    df = _bonus_series(adjusted_by_source=True)
    act = CorporateAction("ABC", df.index[6].date(), "bonus", "Bonus 1:1", ratio_new=1, ratio_held=1)
    res = adjust_ohlcv(df, [act], s.corporate_actions)
    assert res.actions[0].decision == "applied"
    # ex-date is a recorded action date, so the jump there is not reported as unexplained
    assert res.df["close"].iloc[5] == pytest.approx(51.5)


def test_dividend_large_only(settings):
    closes = [100.0] * 5 + [93.0] * 3
    df = flat_frame(closes)
    small = CorporateAction("ABC", df.index[5].date(), "dividend", "Dividend - Rs 2", amount=2.0)
    large = CorporateAction("ABC", df.index[5].date(), "dividend", "Special Dividend - Rs 7", amount=7.0)
    assert adjust_ohlcv(df, [small], settings.corporate_actions).actions[0].decision == "skipped_disabled"
    res = adjust_ohlcv(df, [large], settings.corporate_actions)
    assert res.actions[0].decision == "applied"
    assert res.df["close"].iloc[0] == pytest.approx(93.0)


def test_unexplained_split_gap_warns(settings):
    closes = [500.0] * 5 + [100.0] * 3
    df = frame([(c, c + 1, c - 1, c, 1000) for c in closes])
    res = adjust_ohlcv(df, [], settings.corporate_actions)
    assert len(res.warnings) == 1 and "possible unadjusted" in res.warnings[0]


def test_intraday_uses_daily_decisions(settings):
    df = _bonus_series(adjusted_by_source=False)
    act = CorporateAction("ABC", df.index[6].date(), "bonus", "Bonus 1:1", ratio_new=1, ratio_held=1)
    res = adjust_ohlcv(df, [act], settings.corporate_actions)
    ts = pd.DatetimeIndex(["2026-06-12 09:15", "2026-06-16 09:15"]).tz_localize("Asia/Kolkata")
    intra = pd.DataFrame({"open": [200.0, 104.0], "high": [201.0, 105.0], "low": [199.0, 103.0], "close": [200.0, 104.0], "volume": [10.0, 10.0]}, index=ts)
    adj = adjust_intraday(intra, res)
    assert adj["close"].tolist() == [100.0, 104.0]
    assert adj["volume"].tolist() == [20.0, 10.0]
