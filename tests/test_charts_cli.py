from datetime import date

import pytest

from nseswing import charts
from nseswing.cli import main
from nseswing.data.csvstore import CsvProvider, CsvStore
from nseswing.replay import ReplayEngine
from nseswing.synthetic import reference_scenario, write_scenario

DAY = date(2026, 10, 26)


@pytest.fixture(scope="module")
def replay(tmp_path_factory, settings, calendar):
    store = CsvStore(tmp_path_factory.mktemp("charts"))
    sc = reference_scenario(settings, calendar, DAY)
    write_scenario(store, sc)
    return ReplayEngine(settings, calendar, CsvProvider(store)).run(sc.symbol, DAY, interval=1, days_after=3)


def _names(fig):
    return {t.name for t in fig.data}


def _texts(fig):
    return " | ".join(a.text for a in fig.layout.annotations)


def test_daily_chart_has_every_annotation(replay):
    fig = charts.daily_chart(replay.symbol, replay.history, replay.level_map, replay.row, replay.plan, replay.circuit)
    assert {"EMA9", "EMA21", "EMA50", "Swing highs", "Swing lows", "AVWAP anchor"} <= _names(fig)
    assert any(n.startswith("AVWAP from") for n in _names(fig))
    text = _texts(fig)
    for needle in ("MEDIUM zone", "Entry ₹477.05", "Stop ₹472.85", "3R ₹489.65", "5R ₹498.05", "higher low"):
        assert needle in text, needle
    assert fig.layout.yaxis2.title.text is None  # volume is a separate row, not a second y-axis on the price plot


def test_weekly_and_intraday_charts(replay):
    wk = charts.weekly_chart(replay.symbol, replay.history, replay.level_map, replay.row, replay.plan)
    assert "Advance → pullback (weekly check)" in _names(wk) and "advance +" in _texts(wk)
    fills = replay.position.fills
    intr = charts.intraday_chart(replay.symbol, replay.intraday, 5, replay.level_map, replay.history, replay.plan, fills, replay.trigger_events, replay.circuit, theme="dark")
    assert {"AVWAP (daily anchor)", "Trigger", "Dip into zone", "Entry fill", "Partial (target)"} <= _names(intr)
    assert "open ₹485.76" in _texts(intr)
    # stepping the replay: nothing after 09:30 is drawn yet
    early = charts.intraday_chart(replay.symbol, replay.intraday, 1, replay.level_map, replay.history, replay.plan, fills, replay.trigger_events, replay.circuit, upto=replay.intraday.index[15])
    assert "Trigger" not in _names(early) and "Entry fill" not in _names(early)


def test_report_and_levels_table(tmp_path, replay):
    tbl = charts.levels_table(replay.level_map)
    assert {"zone", "level", "kind", "price"} <= set(tbl.columns) and len(tbl) >= 3
    fig = charts.daily_chart(replay.symbol, replay.history, replay.level_map, replay.row, replay.plan)
    out = charts.write_report(tmp_path / "r.html", "Replay", [fig], replay.plan, [("Levels", tbl)], "cdn", replay.log_lines())
    html = out.read_text()
    assert "Trade plan · REFDEMO.SYN" in html and "Rule log (IST)" in html and "not financial advice" in html


def test_cli_check_config_and_plan(capsys):
    assert main(["check-config"]) == 0
    assert "live orders allowed: False" in capsys.readouterr().out
    assert main(["plan", "ACC", "--entry", "500", "--stop", "487.50"]) == 0
    out = capsys.readouterr().out
    assert "₹2,00,000.00 = 20.00% of account" in out and "3R ₹537.50" in out and "5R ₹562.50" in out
