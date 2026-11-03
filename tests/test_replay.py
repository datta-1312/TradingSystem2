from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from nseswing.alerts import AlertBus
from nseswing.config import load_settings
from nseswing.core.timeutil import IST
from nseswing.data.base import Quote
from nseswing.data.csvstore import CsvProvider, CsvStore
from nseswing.data.reference import ReferenceData, SecurityInfo
from nseswing.monitor import LiveMonitor
from nseswing.pipeline import run_premarket
from nseswing.plancard import render_markdown, render_text
from nseswing.replay import ReplayEngine, ReplayError
from nseswing.synthetic import breakdown_scenario, make_universe, reference_scenario, write_scenario

DAY = date(2026, 10, 26)
D = Decimal


@pytest.fixture(scope="module")
def ref_store(tmp_path_factory, settings, calendar):
    store = CsvStore(tmp_path_factory.mktemp("ref"))
    sc = reference_scenario(settings, calendar, DAY)
    write_scenario(store, sc)
    write_scenario(store, breakdown_scenario(settings, calendar, DAY))
    return store, sc


@pytest.fixture(scope="module")
def replay_1m(settings, calendar, ref_store):
    store, sc = ref_store
    return ReplayEngine(settings, calendar, CsvProvider(store)).run(sc.symbol, DAY, interval=1, days_after=8)


def _first(res, kind):
    return next(a for a in res.alerts if a.kind == kind)


def test_reference_replay_fires_rules_in_order(replay_1m):
    kinds = [a.kind for a in sorted(replay_1m.alerts, key=lambda a: a.ts)]
    order = ["PREOPEN_WATCH", "ZONE_APPROACH", "DIP_INTO_ZONE", "TRIGGER", "PLAN", "ENTRY_FILLED", "STOP_PLACED", "TARGET_PARTIAL", "OVERNIGHT_GTT", "TRAIL_EXIT", "POSITION_CLOSED"]
    positions = [kinds.index(k) for k in order]
    assert positions == sorted(positions)


def test_reference_replay_timings_and_levels(replay_1m):
    trig = _first(replay_1m, "TRIGGER")
    assert pd.Timestamp(trig.ts).strftime("%H:%M") == "09:42"
    plan = replay_1m.plan
    assert plan.trigger_price == D("477.05")  # 09:41 high ₹476.98 + 1 tick, on the ₹0.05 grid
    assert plan.stop == D("472.90") and plan.stop_method == "day_low"
    assert {"ema9", "ema21", "avwap"} <= plan.zone.kinds
    assert _first(replay_1m, "PREOPEN_WATCH").message.startswith("expected open ₹485.76 (+1.20% gap")
    # 3R partial the same day, overnight GTT at 15:30
    partial = _first(replay_1m, "TARGET_PARTIAL")
    assert pd.Timestamp(partial.ts).date() == DAY and "+3.00R" in partial.message
    assert pd.Timestamp(_first(replay_1m, "OVERNIGHT_GTT").ts).strftime("%H:%M") == "15:30"


def test_reference_replay_journal(replay_1m):
    trades = replay_1m.journal.trades()
    assert len(trades) == 1 and trades.loc[0, "status"] == "closed"
    r_gross, r_net = D(trades.loc[0, "r_gross"]), D(trades.loc[0, "r_net"])
    assert r_gross > D("3") and D("0") < r_net < r_gross
    assert D(trades.loc[0, "charges"]) > 0


def test_breakdown_gives_no_trade_and_marks_zone_broken(settings, calendar, ref_store):
    store, _ = ref_store
    res = ReplayEngine(settings, calendar, CsvProvider(store)).run("BRKDEMO.SYN", DAY, interval=1)
    kinds = {a.kind for a in res.alerts}
    assert "TRIGGER" not in kinds and "ENTRY_FILLED" not in kinds
    assert "ZONE_BROKEN" in kinds and res.plan is None and res.position is None


def test_five_minute_chart_triggers_intrabar(settings, calendar, ref_store):
    store, sc = ref_store
    res = ReplayEngine(settings, calendar, CsvProvider(store)).run(sc.symbol, DAY, interval=5)
    assert res.timeframe == 5
    trig = _first(res, "TRIGGER")
    t = pd.Timestamp(trig.ts)
    assert t >= pd.Timestamp(f"{DAY} 09:42", tz=IST) and res.plan is not None
    assert res.plan.trigger_price > D("477.05")  # the previous 5-min bar's high is higher than the 1-min one


def test_replay_rejects_holidays(settings, calendar, ref_store):
    store, sc = ref_store
    with pytest.raises(ReplayError):
        ReplayEngine(settings, calendar, CsvProvider(store)).run(sc.symbol, date(2026, 10, 20))  # Dussehra


def test_plan_card_rendering(replay_1m):
    text = render_text(replay_1m.plan)
    for needle in ("TRADE PLAN · REFDEMO.SYN", "Zone", "Trigger", "Stop", "Shares", "3R ₹", "5R ₹", "Circuits", "Flags", "not financial advice"):
        assert needle in text
    md = render_markdown(replay_1m.plan)
    assert md.startswith("### Trade plan · REFDEMO.SYN") and "| **Position** |" in md


def test_premarket_pipeline_on_synthetic_universe(settings, calendar):
    data, profiles = make_universe(calendar, date(2026, 10, 30))
    ref = ReferenceData({s: SecurityInfo(s, s, industry=p.industry, band_pct=20.0, band_source="sec_list") for s, p in profiles.items()})
    probe = run_premarket(settings, calendar, None, ref, data=data, as_of=date(2026, 10, 30))
    quotes = {}
    for sym, lm in probe.level_maps.items():
        top = float(max(lm.support_zones(), key=lambda z: z.top).top)
        quotes[sym] = Quote(sym, ltp=top * 1.01, prev_close=lm.last_close)
    run = run_premarket(settings, calendar, None, ref, data=data, as_of=date(2026, 10, 30), quotes=quotes)
    assert {c.symbol for c in run.candidates} == {"ALPHA.SYN", "BRAVO.SYN", "CHARLIE.SYN"}
    assert set(run.drafts) == {"ALPHA.SYN", "BRAVO.SYN", "CHARLIE.SYN"} and all(p.is_draft for p in run.drafts.values())


class FakeClock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=max(seconds, 15))


class TickFeed:
    """Serves LTP from 1-minute bars: open, then low/high (by bar colour), then close, through the minute."""

    def __init__(self, bars: pd.DataFrame, clock: FakeClock, symbol: str):
        self.bars, self.clock, self.symbol = bars, clock, symbol
        self.cum = 0.0

    def quotes(self, symbols):
        first = self.bars.iloc[0]
        return {self.symbol: Quote(self.symbol, ltp=float(first["open"]))}

    def ltp(self, symbols):
        t = pd.Timestamp(self.clock())
        minute = t.floor("min")
        if minute not in self.bars.index:
            return {}
        row = self.bars.loc[minute]
        sec = t.second
        green = row["close"] >= row["open"]
        seq = [row["open"], row["low"] if green else row["high"], row["high"] if green else row["low"], row["close"]]
        self.cum += row["volume"] / 4
        return {self.symbol: (float(seq[min(3, sec // 15)]), self.cum)}

    def daily(self, symbol, start, end):
        return pd.DataFrame()


def test_live_monitor_paper_session_with_fake_feed(tmp_path, calendar, ref_store):
    store, sc = ref_store
    s = load_settings(overrides={
        "data": {"state_dir": str(tmp_path / "state")},
        "execution": {"paper": {"state_file": str(tmp_path / "state" / "paper.json")}},
        "journal": {"db_path": str(tmp_path / "journal.sqlite")},
        "intraday": {"timeframe_minutes": 1},
    })
    hist = sc.daily[sc.daily.index < pd.Timestamp(DAY)]
    ref = ReferenceData({sc.symbol: SecurityInfo(sc.symbol, sc.symbol, band_pct=20.0, band_source="sec_list")})
    run = run_premarket(s, calendar, None, ref, data={sc.symbol: hist}, as_of=hist.index[-1].date(), quotes={sc.symbol: Quote(sc.symbol, ltp=485.76)})
    clock = FakeClock(datetime(2026, 10, 26, 9, 9, 0, tzinfo=IST))
    feed = TickFeed(sc.intraday[DAY], clock, sc.symbol)
    alerts = AlertBus(s, console=False, file=None)
    mon = LiveMonitor(s, calendar, feed, run, ref, alerts, clock=clock, sleep=clock.sleep, day=DAY)
    mon.run()
    kinds = alerts.kinds()
    assert "TRIGGER" in kinds and "ENTRY_FILLED" in kinds and "STOP_PLACED" in kinds and "OVERNIGHT_GTT" in kinds
    trig = next(a for a in alerts.alerts if a.kind == "TRIGGER")
    assert pd.Timestamp(trig.ts).strftime("%H:%M") == "09:42"
    assert (tmp_path / "state" / "positions.json").exists() and (tmp_path / "state" / "paper.json").exists()
