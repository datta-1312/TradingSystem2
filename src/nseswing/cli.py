"""Command line: ``nseswing <command>``. Run ``nseswing --help`` for the list."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from nseswing.config import Settings, load_settings


# ------------------------------------------------------------------ shared helpers
def _settings(args) -> Settings:
    overrides = {}
    if getattr(args, "provider", None):
        overrides["data"] = {"provider": args.provider}
    return load_settings(args.config, overrides or None)


def _calendar(s: Settings):
    from nseswing.core.calendar import NSECalendar

    return NSECalendar.from_settings(s)


def _reference(s: Settings, provider_symbols: list[str] | None = None):
    from nseswing.data.reference import ReferenceData, SecurityInfo

    try:
        return ReferenceData.load(s)
    except FileNotFoundError as exc:
        if provider_symbols:
            print(f"note: {exc}; using bare reference data for {len(provider_symbols)} CSV symbols", file=sys.stderr)
            return ReferenceData({sym: SecurityInfo(sym, sym) for sym in provider_symbols}, [str(exc)])
        raise SystemExit(str(exc))


def _provider(s: Settings, reference=None):
    from nseswing.data.csvstore import CsvProvider, CsvStore

    if s.data.provider == "csv":
        return CsvProvider(CsvStore(s.path(s.data.csv_dir)))
    from nseswing.data.upstox import UpstoxClient, UpstoxProvider

    inst_file = s.path(s.data.reference_dir) / "upstox_instruments_nse.json"
    instruments = UpstoxClient.load_instruments(inst_file) if inst_file.exists() else {}
    return UpstoxProvider(s, instruments=instruments, reference=reference, cache=CsvStore(s.path(s.data.cache_dir)))


def _actions(s: Settings):
    from nseswing.corpactions import load_actions

    return load_actions(s.path(s.corporate_actions.actions_file), s.path(s.corporate_actions.manual_actions_file))


def _as_of(s: Settings, cal, text: str | None) -> date:
    if text:
        return date.fromisoformat(text)
    from nseswing.core.timeutil import now_ist

    return cal.last_completed_session(now_ist())


def _setup(args):
    s = _settings(args)
    cal = _calendar(s)
    csv_syms = None
    if s.data.provider == "csv":
        from nseswing.data.csvstore import CsvStore

        csv_syms = CsvStore(s.path(s.data.csv_dir)).daily_symbols()
    ref = _reference(s, csv_syms)
    return s, cal, ref, _provider(s, ref)


# ------------------------------------------------------------------ commands
def cmd_check_config(args) -> int:
    from nseswing.config import leaf_keys, load_raw

    s = load_settings(args.config)
    keys = leaf_keys(load_raw(args.config))
    print(f"settings OK: {len(keys)} keys, mode={s.execution.mode}, provider={s.data.provider}, account {s.account.equity_inr} @ {s.account.risk_per_trade_pct}% risk, product {s.account.product}")
    print("live orders allowed:", s.live_orders_allowed)
    return 0


def cmd_upstox_login(args) -> int:
    from nseswing.data.upstox import UpstoxClient

    s = _settings(args)
    client = UpstoxClient(s)
    if not args.code:
        print("1. open this URL, log in, and copy the `code` parameter from the redirect:\n")
        print("  ", client.authorization_url())
        print("\n2. run:  nseswing upstox-login --code <CODE>")
        return 0
    client.exchange_code(args.code)
    print(f"token saved to {s.path(s.upstox.token_file)} (valid until 03:30 IST tomorrow)")
    return 0


def cmd_refresh(args) -> int:
    from nseswing.data.refresh import refresh_reference

    s = _settings(args)
    failed = 0
    for r in refresh_reference(s, _calendar(s)):
        failed += not r.ok
        print(f"{'OK  ' if r.ok else 'FAIL'} {r.item:<28} {r.detail}")
    return 1 if failed else 0


def _premarket(args, quotes=None):
    from nseswing.pipeline import run_premarket

    s, cal, ref, prov = _setup(args)
    as_of = _as_of(s, cal, getattr(args, "as_of", None))
    symbols = args.symbols if getattr(args, "symbols", None) else None
    run = run_premarket(s, cal, prov, ref, _actions(s), as_of=as_of, symbols=symbols, quotes=quotes)
    return s, cal, ref, prov, run


def cmd_screen(args) -> int:
    s, cal, ref, prov, run = _premarket(args)
    tbl = run.screen.table(watchlist_only=not args.all)
    out = s.path(s.charts.output_dir).parent / f"watchlist_{run.as_of}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    tbl.to_csv(out, index=False)
    with pd.option_context("display.width", 220, "display.max_columns", 30, "display.max_colwidth", 60):
        cols = ["rank", "symbol", "tier", "hot_groups", "ret_m1_%", "ret_m3_%", "ret_m6_%", "turnover_cr", "weekly", "7a_higher_lows", "7b_contraction", "7c_ema", "8_disqualified", "all_pass", "flags"]
        print(tbl[[c for c in cols if c in tbl.columns]].to_string(index=False) if not tbl.empty else "watchlist is empty")
    print(f"\nhot groups: {', '.join(run.screen.hot_groups) or 'none'}")
    for n in run.notes[:20]:
        print("note:", n)
    print(f"full table with every reason: {out}")
    return 0


def cmd_zones(args) -> int:
    s, cal, ref, prov, run = _premarket(args)
    for sym, lm in run.level_maps.items():
        print(f"\n{sym}  (last close ₹{lm.last_close:.2f}, tick ₹{lm.tick})")
        for z in lm.zones:
            print("   ", z.describe())
        for a in lm.avwaps:
            print(f"    {a.label}: ₹{float(a.series.iloc[-1]):.2f}, state {a.state.get('state')}")
    return 0


def cmd_preopen(args) -> int:
    from nseswing.plancard import render_text

    s, cal, ref, prov = _setup(args)
    quotes = None
    try:
        probe = __import__("nseswing.pipeline", fromlist=["run_premarket"]).run_premarket(s, cal, prov, ref, _actions(s), as_of=_as_of(s, cal, args.as_of))
        syms = [r.symbol for r in probe.screen.watchlist if r.all_checks_pass]
        quotes = prov.quotes(syms) if syms else {}
    except Exception as exc:  # noqa: BLE001
        print(f"note: no live quotes ({exc}); expected open = previous close", file=sys.stderr)
        probe = None
    from nseswing.pipeline import run_premarket

    preopen = None
    if s.preopen.expected_open_source == "nse_preopen":
        from nseswing.data.nse import fetch_preopen

        try:
            preopen = fetch_preopen(s)
        except Exception as exc:  # noqa: BLE001
            print(f"note: NSE pre-open data unavailable ({exc})", file=sys.stderr)
    run = run_premarket(s, cal, prov, ref, _actions(s), as_of=_as_of(s, cal, args.as_of), quotes=quotes, preopen=preopen, data=probe.data if probe else None)
    if not run.candidates:
        print("no watchlist stock has a support zone near the expected open")
    for c in run.candidates:
        print(f"\n{c.symbol}: {c.status.upper()}  expected open ₹{c.expected_open:.2f} ({c.gap_pct:+.2f}%, {c.source})  {c.note}")
        if c.symbol in run.drafts:
            print(render_text(run.drafts[c.symbol]))
    return 0


def cmd_plan(args) -> int:
    from nseswing.core.ticks import TickTable
    from nseswing.plancard import render_text
    from nseswing.risk import RiskCalculator

    s = _settings(args)
    rc = RiskCalculator(s, TickTable.from_settings(s))
    plan = rc.plan(args.symbol, Decimal(args.entry), Decimal(args.stop), reference_price=Decimal(args.ref) if args.ref else None)
    print(render_text(plan))
    return 0


def _report_from_replay(s: Settings, res, out: Path) -> Path:
    from nseswing import charts

    fills = res.position.fills if res.position else []
    figs = [
        charts.intraday_chart(res.symbol, res.intraday, res.timeframe, res.level_map, res.history, res.plan, fills, res.trigger_events, res.circuit),
        charts.daily_chart(res.symbol, res.history, res.level_map, res.row, res.plan, res.circuit, bars=s.charts.daily_bars_shown, height=s.charts.height_px),
        charts.weekly_chart(res.symbol, res.history, res.level_map, res.row, res.plan, weeks=s.charts.weekly_bars_shown),
    ]
    tables = [("Support zones and levels (table view of the charts)", charts.levels_table(res.level_map))]
    trades = res.journal.trades()
    if not trades.empty:
        tables.append(("Journal", trades))
        tables.append(("Fills and charges", res.journal.fills()))
    return charts.write_report(out, f"Replay · {res.symbol} · {res.day} · {res.timeframe}-min", figs, res.plan, tables, s.charts.plotly_js, res.log_lines())


def cmd_replay(args) -> int:
    from nseswing.levels import load_manual_anchors
    from nseswing.replay import ReplayEngine

    s, cal, ref, prov = _setup(args)
    anchors = load_manual_anchors(s.path(s.levels.avwap.manual_anchors_file))
    eng = ReplayEngine(s, cal, prov, ref, _actions(s), manual_anchors=anchors)
    res = eng.run(args.symbol, date.fromisoformat(args.date), interval=args.interval, days_after=args.days_after)
    print("\n".join(res.log_lines()))
    for n in res.notes:
        print("note:", n)
    out = s.path(s.charts.output_dir) / f"replay_{args.symbol}_{args.date}_{res.timeframe}m.html"
    print(f"\nreport with annotated charts: {_report_from_replay(s, res, out)}")
    return 0


def cmd_demo(args) -> int:
    from nseswing.data.csvstore import CsvProvider, CsvStore
    from nseswing.replay import ReplayEngine
    from nseswing.synthetic import breakdown_scenario, reference_scenario, write_scenario

    s = load_settings(args.config)
    cal = _calendar(s)
    day = date.fromisoformat(args.day)
    root = s.path("data/demo")
    store = CsvStore(root)
    sc = reference_scenario(s, cal, day)
    write_scenario(store, sc)
    write_scenario(store, breakdown_scenario(s, cal, day))
    print("SYNTHETIC DEMO DATA (fictional symbols, not market data)")
    print(sc.description, "\n")
    eng = ReplayEngine(s, cal, CsvProvider(store))
    res = eng.run(sc.symbol, day, interval=args.interval, days_after=8)
    print("\n".join(res.log_lines()))
    out = s.path(s.charts.output_dir) / f"demo_{sc.symbol}_{day}.html"
    print(f"\nreport with annotated charts: {_report_from_replay(s, res, out)}")
    brk = eng.run("BRKDEMO.SYN", day, interval=args.interval)
    print("\nbreakdown scenario (price falls through the zone):")
    print("\n".join(l for l in brk.log_lines() if any(k in l for k in ("ZONE_BROKEN", "NEXT_ZONE", "NO_ZONES", "TRIGGER"))))
    return 0


def cmd_monitor(args) -> int:
    from nseswing.alerts import AlertBus
    from nseswing.monitor import LiveMonitor
    from nseswing.pipeline import run_premarket

    s, cal, ref, prov = _setup(args)
    gateway = None
    if s.execution.mode == "live":
        from nseswing.data.upstox import UpstoxClient
        from nseswing.orders.upstox_live import UpstoxLiveGateway

        keys = {sym: prov.instrument_key(sym) for sym in ref.symbols()}
        gateway = UpstoxLiveGateway(s, UpstoxClient(s), keys)  # raises unless every live switch is on
    run = run_premarket(s, cal, prov, ref, _actions(s), as_of=_as_of(s, cal, args.as_of))
    print(f"monitoring {len(run.candidates)} pre-open candidates ({s.execution.mode} mode, {s.intraday.timeframe_minutes}-min chart)")
    LiveMonitor(s, cal, prov, run, ref, AlertBus(s), gateway=gateway).run()
    return 0


def cmd_journal(args) -> int:
    from nseswing.journal import Journal

    s = _settings(args)
    j = Journal(s.path(s.journal.db_path))
    trades = j.trades()
    with pd.option_context("display.width", 220, "display.max_columns", 30):
        print(trades.to_string(index=False) if not trades.empty else "journal is empty")
    if args.export:
        for p in j.export_csv(Path(args.export)):
            print("wrote", p)
    return 0


def cmd_hold_full(args) -> int:
    s = _settings(args)
    path = s.path(s.data.state_dir) / "positions.json"
    if not path.exists():
        raise SystemExit("no saved paper positions")
    rows = json.loads(path.read_text())
    hit = [r for r in rows if r["symbol"] == args.symbol and r["status"] == "open"]
    if not hit:
        raise SystemExit(f"no open position in {args.symbol}")
    for r in hit:
        r["hold_full"] = args.state == "on"
    path.write_text(json.dumps(rows, indent=1))
    print(f"{args.symbol}: hold-full override {'ON (partials skipped)' if args.state == 'on' else 'OFF'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nseswing", description="Pullback swing-trading decision support for NSE cash equities (not financial advice)")
    ap.add_argument("--config", default=None, help="path to settings.yaml (default: config/settings.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check-config", help="validate settings.yaml").set_defaults(fn=cmd_check_config)
    p = sub.add_parser("upstox-login", help="daily Upstox OAuth login")
    p.add_argument("--code")
    p.set_defaults(fn=cmd_upstox_login)
    sub.add_parser("refresh-reference", help="download NSE/Upstox reference files").set_defaults(fn=cmd_refresh)
    for name, fn, helptext in (("screen", cmd_screen, "Stage 1 ranked watchlist"), ("zones", cmd_zones, "Stage 2 support zones"), ("preopen", cmd_preopen, "near-zone list and draft plans"), ("monitor", cmd_monitor, "live/paper session monitor")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--as-of", dest="as_of")
        p.add_argument("--provider", choices=["upstox", "csv"])
        p.add_argument("--symbols", nargs="*")
        if name == "screen":
            p.add_argument("--all", action="store_true", help="show every stock, not just the watchlist")
        p.set_defaults(fn=fn)
    p = sub.add_parser("plan", help="trade-plan card for an entry and stop")
    p.add_argument("symbol")
    p.add_argument("--entry", required=True)
    p.add_argument("--stop", required=True)
    p.add_argument("--ref", help="reference price that sets the tick slab (default: entry)")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("replay", help="step through a session bar by bar")
    p.add_argument("symbol")
    p.add_argument("date")
    p.add_argument("--interval", type=int, choices=[1, 5, 15])
    p.add_argument("--days-after", type=int, default=10)
    p.add_argument("--provider", choices=["upstox", "csv"])
    p.set_defaults(fn=cmd_replay)
    p = sub.add_parser("demo", help="run the synthetic reference scenario end to end")
    p.add_argument("--day", default="2026-10-26")
    p.add_argument("--interval", type=int, choices=[1, 5, 15], default=1)
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("journal", help="show the trade journal")
    p.add_argument("--export")
    p.set_defaults(fn=cmd_journal)
    p = sub.add_parser("hold-full", help="manual override: hold the full position (skip partials)")
    p.add_argument("symbol")
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(fn=cmd_hold_full)
    args = ap.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
