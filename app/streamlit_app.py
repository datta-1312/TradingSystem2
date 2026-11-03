"""nseswing dashboard: `streamlit run app/streamlit_app.py`

Tabs: Watchlist · Setup charts · Pre-open · Replay · Journal · Settings.
Data source (sidebar): the synthetic demo (works offline), a CSV folder, or Upstox.
Decision support only, not financial advice. Live orders are never sent from here.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from nseswing import charts
from nseswing.config import load_settings
from nseswing.core.calendar import NSECalendar
from nseswing.core.money import format_inr
from nseswing.data.csvstore import CsvProvider, CsvStore
from nseswing.data.reference import ReferenceData, SecurityInfo
from nseswing.journal import Journal
from nseswing.pipeline import run_premarket
from nseswing.plancard import render_markdown
from nseswing.replay import ReplayEngine, ReplayError
from nseswing.synthetic import breakdown_scenario, make_universe, reference_scenario, write_scenario

st.set_page_config(page_title="nseswing", layout="wide")
SETTINGS = load_settings()
CAL = NSECalendar.from_settings(SETTINGS)
DEMO_DAY = date(2026, 10, 26)


def theme_name() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # noqa: BLE001
        return "light"


# ------------------------------------------------------------------ data sources
@st.cache_resource(show_spinner="Building the synthetic demo (fictional symbols)…")
def demo_source():
    root = SETTINGS.path("data/demo")
    store = CsvStore(root)
    data, profiles = make_universe(CAL, CAL.previous_trading_day(DEMO_DAY))
    for sym, df in data.items():
        store.write_daily(sym, df)
    sc = reference_scenario(SETTINGS, CAL, DEMO_DAY)
    write_scenario(store, sc)
    write_scenario(store, breakdown_scenario(SETTINGS, CAL, DEMO_DAY))
    secs = {s: SecurityInfo(s, s, industry=p.industry, band_pct=20.0, band_source="sec_list") for s, p in profiles.items()}
    secs[sc.symbol] = SecurityInfo(sc.symbol, sc.symbol, industry="Capital Goods", band_pct=20.0, band_source="sec_list")
    secs["BRKDEMO.SYN"] = SecurityInfo("BRKDEMO.SYN", "BRKDEMO.SYN", industry="Capital Goods", band_pct=20.0, band_source="sec_list")
    return CsvProvider(store), ReferenceData(secs), CAL.previous_trading_day(DEMO_DAY)


def csv_source(folder: str):
    store = CsvStore(Path(folder))
    try:
        ref = ReferenceData.load(SETTINGS)
    except FileNotFoundError:
        ref = ReferenceData({s: SecurityInfo(s, s) for s in store.daily_symbols()})
    return CsvProvider(store), ref


def upstox_source():
    from nseswing.cli import _provider

    ref = ReferenceData.load(SETTINGS)
    return _provider(SETTINGS, ref), ref


st.sidebar.title("nseswing")
st.sidebar.caption("Pullback swing setups · NSE cash · decision support, not advice")
source = st.sidebar.radio("Data source", ["Synthetic demo", "CSV folder", "Upstox"], help="The demo uses fictional .SYN symbols and needs no network.")
as_of = None
try:
    if source == "Synthetic demo":
        provider, reference, as_of = demo_source()
        st.sidebar.info("SYNTHETIC data: fictional symbols, not market data.")
    elif source == "CSV folder":
        folder = st.sidebar.text_input("Folder", str(SETTINGS.path(SETTINGS.data.csv_dir)))
        provider, reference = csv_source(folder)
    else:
        provider, reference = upstox_source()
except Exception as exc:  # noqa: BLE001
    st.sidebar.error(f"data source unavailable: {exc}")
    st.stop()
as_of = st.sidebar.date_input("Screen as of (last completed session)", as_of or CAL.previous_trading_day(date.today() + timedelta(days=1)))
st.sidebar.write(f"Mode: **{SETTINGS.execution.mode}** · account {format_inr(SETTINGS.account.equity_inr, 0)} · risk {SETTINGS.account.risk_per_trade_pct}% · {SETTINGS.account.product}")


@st.cache_data(show_spinner="Screening (Stage 1) and marking zones (Stage 2)…", hash_funcs={CsvProvider: id, ReferenceData: id})
def premarket(_provider, _reference, as_of_day: date, source_key: str):
    syms = _reference.symbols()
    return run_premarket(SETTINGS, CAL, _provider, _reference, [], as_of=as_of_day, symbols=syms)


try:
    run = premarket(provider, reference, as_of, source)
except Exception as exc:  # noqa: BLE001
    st.error(f"pre-market run failed: {exc}")
    st.stop()

tabs = st.tabs(["Watchlist", "Setup charts", "Pre-open", "Replay", "Journal", "Settings"])

# ------------------------------------------------------------------ watchlist
with tabs[0]:
    st.subheader(f"Stage 1 watchlist · as of {run.as_of}")
    show_all = st.toggle("Show every stock (not only the watchlist)")
    tbl = run.screen.table(watchlist_only=not show_all)
    if tbl.empty:
        st.write("No stock passed momentum and liquidity.")
    else:
        st.dataframe(tbl, hide_index=True, width="stretch", height=min(38 + 35 * len(tbl), 600))
    st.caption("Hot groups (≥ {} momentum passers): {}".format(SETTINGS.screener.themes.min_members_hot, ", ".join(run.screen.hot_groups) or "none"))
    for n in run.notes[:15]:
        st.caption(f"note: {n}")

# ------------------------------------------------------------------ setup charts
with tabs[1]:
    ready = sorted(run.level_maps)
    if not ready:
        st.write("No stock passes every Stage 1 check today.")
    else:
        sym = st.selectbox("Setup", ready)
        lm, row, df = run.level_maps[sym], run.screen.get(sym), run.data[sym]
        circuit = reference.circuit_limits(sym, lm.last_close, lm.tick, SETTINGS)
        plan = run.drafts.get(sym)
        th = theme_name()
        st.plotly_chart(charts.daily_chart(sym, df, lm, row, plan, circuit, theme=th, bars=SETTINGS.charts.daily_bars_shown, height=SETTINGS.charts.height_px), width="stretch", theme=None)
        st.plotly_chart(charts.weekly_chart(sym, df, lm, row, plan, theme=th, weeks=SETTINGS.charts.weekly_bars_shown), width="stretch", theme=None)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Stage 1 checks**")
            st.dataframe(pd.DataFrame([{"check": c.name, "result": "PASS" if c.passed else "FAIL", "reason": c.reason} for c in row.checks()]), hide_index=True, width="stretch")
            st.markdown("**Zones and levels** (table view of the chart)")
            st.dataframe(charts.levels_table(lm), hide_index=True, width="stretch")
        with c2:
            if plan is not None:
                st.markdown(render_markdown(plan))
            else:
                st.write("No draft plan: no zone near the expected open.")

# ------------------------------------------------------------------ pre-open
with tabs[2]:
    st.subheader("Stocks whose support zones sit near the expected open")
    st.caption("Expected open = pre-open price discovery (Upstox quote / NSE IEP) when available, else the previous close.")
    if not run.candidates:
        st.write("None today.")
    for c in run.candidates:
        with st.expander(f"{c.symbol} · {c.status.upper()} · expected open ₹{c.expected_open:.2f} ({c.gap_pct:+.2f}%)"):
            st.write(c.note)
            if c.symbol in run.drafts:
                st.markdown(render_markdown(run.drafts[c.symbol]))

# ------------------------------------------------------------------ replay
with tabs[3]:
    st.subheader("Replay: step through a session bar by bar (IST)")
    cols = st.columns(4)
    default_sym = "REFDEMO.SYN" if source == "Synthetic demo" else (ready[0] if ready else "")
    r_sym = cols[0].text_input("Symbol", default_sym)
    r_day = cols[1].date_input("Session", DEMO_DAY if source == "Synthetic demo" else run.as_of)
    r_tf = cols[2].selectbox("Chart", [1, 5, 15], index=0, format_func=lambda m: f"{m}-min")
    r_after = cols[3].number_input("Manage for N later sessions", 0, 30, 8)
    if st.button("Run replay", type="primary"):
        try:
            res = ReplayEngine(SETTINGS, CAL, provider, reference).run(r_sym, r_day, interval=r_tf, days_after=int(r_after))
            st.session_state["replay"] = res
            trig = next((a for a in res.alerts if a.kind == "TRIGGER"), None)
            idx = res.intraday.index
            st.session_state["replay_step"] = int(idx.searchsorted(pd.Timestamp(trig.ts))) if trig else len(idx) - 1
        except ReplayError as exc:
            st.error(str(exc))
    res = st.session_state.get("replay")
    if res is not None:
        bars = res.intraday.index
        last = len(bars) - 1

        def _nudge(delta: int) -> None:
            st.session_state["replay_step"] = min(last, max(0, st.session_state.get("replay_step", last) + delta))

        st.slider("Bar (1-minute steps; the chart re-draws on the selected timeframe)", 0, last, key="replay_step")
        b1, b2, b3, _ = st.columns([1, 1, 1, 5])
        b1.button("◀ previous bar", on_click=_nudge, args=(-1,))
        b2.button("next bar ▶", on_click=_nudge, args=(1,))
        b3.button("end of day ⏭", on_click=_nudge, args=(last,))
        step = st.session_state["replay_step"]
        upto = bars[step]
        st.caption(f"Showing up to {upto:%H:%M} IST")
        fills = res.position.fills if res.position else []
        st.plotly_chart(charts.intraday_chart(res.symbol, res.intraday, res.timeframe, res.level_map, res.history, res.plan, fills, res.trigger_events, res.circuit, theme=theme_name(), upto=upto), width="stretch", theme=None)
        so_far = [a for a in sorted(res.alerts, key=lambda a: a.ts) if pd.Timestamp(a.ts) <= upto + pd.Timedelta(minutes=1)]
        st.markdown("**Rules fired so far**")
        st.dataframe(pd.DataFrame([{"time (IST)": pd.Timestamp(a.ts).strftime("%d-%b %H:%M"), "level": a.level, "rule": a.kind, "detail": a.message} for a in so_far]), hide_index=True, width="stretch")
        if res.plan is not None and pd.Timestamp(res.plan.created_at) <= upto:
            st.markdown(render_markdown(res.plan))
        with st.expander("Full log incl. later sessions"):
            st.code("\n".join(res.log_lines()), language=None)
        trades = res.journal.trades()
        if not trades.empty:
            st.markdown("**Paper result**")
            st.dataframe(trades, hide_index=True, width="stretch")

# ------------------------------------------------------------------ journal
with tabs[4]:
    st.subheader("Trade journal (paper / live)")
    jpath = SETTINGS.path(SETTINGS.journal.db_path)
    if jpath.exists():
        j = Journal(jpath)
        st.dataframe(j.trades(), hide_index=True, width="stretch")
        st.markdown("**Fills with charges**")
        st.dataframe(j.fills(), hide_index=True, width="stretch")
    else:
        st.write("No journal yet. Paper trades from `nseswing monitor` are recorded here; replay results are shown on the Replay tab.")

# ------------------------------------------------------------------ settings
with tabs[5]:
    st.subheader("Settings (read-only here: edit config/settings.yaml)")
    live = SETTINGS.execution.live
    st.write(f"Live orders allowed: **{SETTINGS.live_orders_allowed}** (mode={SETTINGS.execution.mode}, enabled={live.enabled}, confirmed={live.confirm_live_trading}, static IP declared={live.static_ip_registered})")
    st.code(SETTINGS.path("config/settings.yaml").read_text(encoding="utf-8"), language="yaml")
