"""Stage 1: screen for strong stocks that are consolidating.

Steps (method numbering):

1. Returns over 1, 3 and 6 months (21 / 63 / 126 sessions); universe ranked per window.
2. Momentum: in at least one window, return ≥ 30% OR top 30% rank. Stocks
   meeting both conditions are listed first.
3. Liquidity: 50-day average turnover, average volume and price minimums.
4. Survivors form the watchlist.
5. Hot themes: industry / thematic-index / custom groups with ≥ 3 momentum
   passers are prioritised, and within them the fastest risers.
6. Weekly: a prior advance followed by a pullback.
7. Daily: (a) higher pullback lows, (b) contracting range, (c) close has
   reclaimed the 9 or 21 EMA and that EMA is flat or rising.
8. Disqualify falling highs + falling lows with price below declining averages.

Every check returns a :class:`CheckResult` with a human-readable reason, so the
watchlist shows *why* a stock passed or failed. All thresholds come from
``settings.yaml → screener``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd

from nseswing.core.ticks import TickTable
from nseswing.indicators import ema, resample_weekly, slope_pct_per_day, swing_points, true_range_pct

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings
    from nseswing.data.base import Quote
    from nseswing.data.reference import ReferenceData

WINDOWS = ("m1", "m3", "m6")


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def short(self) -> str:
        return ("PASS" if self.passed else "FAIL") + f": {self.reason}"


@dataclass
class Consolidation:
    """The base the daily checks looked at (reused by Stage 2 for the default AVWAP anchor)."""

    high_pos: int
    high_date: pd.Timestamp
    high_price: float
    base_days: int


@dataclass
class ScreenRow:
    symbol: str
    name: str = ""
    industry: str | None = None
    groups: list[str] = field(default_factory=list)
    hot_groups: list[str] = field(default_factory=list)
    returns: dict[str, float] = field(default_factory=dict)  # %
    rank_pct: dict[str, float] = field(default_factory=dict)  # 0 = best
    top: dict[str, bool] = field(default_factory=dict)
    momentum: CheckResult | None = None
    both_conditions: bool = False
    liquidity: CheckResult | None = None
    weekly: CheckResult | None = None
    higher_lows: CheckResult | None = None
    contraction: CheckResult | None = None
    ema_reclaim: CheckResult | None = None
    disqualified: CheckResult | None = None
    flags: list[str] = field(default_factory=list)
    excluded_reason: str | None = None
    consolidation: Consolidation | None = None
    last_close: float | None = None
    skipped_reason: str | None = None

    @property
    def on_watchlist(self) -> bool:
        return bool(self.momentum and self.momentum.passed and self.liquidity and self.liquidity.passed and not self.excluded_reason)

    @property
    def daily_checks(self) -> list[CheckResult]:
        return [c for c in (self.weekly, self.higher_lows, self.contraction, self.ema_reclaim) if c is not None]

    @property
    def all_checks_pass(self) -> bool:
        if not self.on_watchlist or self.skipped_reason:
            return False
        if not all(c.passed for c in self.daily_checks) or len(self.daily_checks) < 4:
            return False
        return not (self.disqualified and self.disqualified.passed)

    @property
    def stage1_conditions_pass(self) -> bool:
        """Daily conditions a trigger must still satisfy (Stage 2.7)."""
        return self.all_checks_pass

    def fastest(self, metric: str) -> float:
        if metric == "best_rank":
            vals = [-v for v in self.rank_pct.values() if not math.isnan(v)]
            return max(vals) if vals else float("-inf")
        v = self.returns.get(metric.replace("ret_", ""), float("nan"))
        return float("-inf") if math.isnan(v) else v

    def checks(self) -> list[CheckResult]:
        return [c for c in (self.momentum, self.liquidity, self.weekly, self.higher_lows, self.contraction, self.ema_reclaim, self.disqualified) if c]


@dataclass
class ScreenResult:
    rows: list[ScreenRow]
    hot_groups: dict[str, list[str]]  # group → momentum passers
    as_of: pd.Timestamp | None

    @property
    def watchlist(self) -> list[ScreenRow]:
        return [r for r in self.rows if r.on_watchlist]

    def get(self, symbol: str) -> ScreenRow | None:
        return next((r for r in self.rows if r.symbol == symbol), None)

    def table(self, watchlist_only: bool = True) -> pd.DataFrame:
        recs = []
        rows = self.watchlist if watchlist_only else self.rows
        for i, r in enumerate(rows, 1):
            rec: dict[str, Any] = {"rank": i, "symbol": r.symbol, "name": r.name, "industry": r.industry}
            rec["hot_groups"] = ", ".join(g.split(": ", 1)[-1] for g in r.hot_groups)
            rec["tier"] = "both" if r.both_conditions else ("one" if r.momentum and r.momentum.passed else "-")
            for w in WINDOWS:
                rec[f"ret_{w}_%"] = round(r.returns.get(w, float("nan")), 1)
            rec["turnover_cr"] = r.liquidity.details.get("avg_turnover_cr") if r.liquidity else None
            for label, c in (
                ("weekly", r.weekly),
                ("7a_higher_lows", r.higher_lows),
                ("7b_contraction", r.contraction),
                ("7c_ema", r.ema_reclaim),
            ):
                rec[label] = ("PASS" if c.passed else "FAIL") if c else "-"
            rec["8_disqualified"] = ("YES" if r.disqualified.passed else "no") if r.disqualified else "-"
            rec["all_pass"] = r.all_checks_pass
            rec["flags"] = "; ".join(r.flags)
            rec["reasons"] = " | ".join(f"{c.name}: {c.reason}" for c in r.checks())
            recs.append(rec)
        return pd.DataFrame(recs)


# ------------------------------------------------------------------ individual checks (pure functions)
def higher_lows(prior_low: float, latest_low: float) -> bool:
    """Stage 1.7a: the latest pullback low is above the prior pullback low."""
    return latest_low > prior_low


def momentum_check(returns: dict[str, float], top: dict[str, bool], cfg) -> tuple[CheckResult, bool]:
    m = cfg.screener.momentum
    passing, both = [], []
    for w in WINDOWS:
        r = returns.get(w, float("nan"))
        if math.isnan(r):
            continue
        abs_ok, rel_ok = r >= m.min_return_pct, top.get(w, False)
        if abs_ok or rel_ok:
            passing.append(f"{w} {r:+.1f}%{' top' if rel_ok else ''}")
        if abs_ok and rel_ok:
            both.append(w)
    if m.both_conditions_scope == "same_window":
        both_ok = bool(both)
    else:
        both_ok = any(returns.get(w, float("nan")) >= m.min_return_pct for w in WINDOWS) and any(top.values())
    if passing:
        reason = "; ".join(passing) + (" (meets both)" if both_ok else "")
        return CheckResult("momentum", True, reason, {"windows": passing, "both_windows": both}), both_ok
    best = max((returns.get(w, float("nan")) for w in WINDOWS), key=lambda x: -1e9 if math.isnan(x) else x)
    return CheckResult("momentum", False, f"no window ≥ {m.min_return_pct:.0f}% or top {m.top_rank_pct:.0f}% (best {best:+.1f}%)"), False


def liquidity_check(df: pd.DataFrame, cfg) -> CheckResult:
    lq = cfg.screener.liquidity
    tail = df.iloc[-lq.avg_days :]
    price = tail["close"] if cfg.data.turnover_price == "close" else (tail["high"] + tail["low"] + tail["close"]) / 3
    turnover_cr = float((price * tail["volume"]).mean() / 1e7)
    avg_vol = float(tail["volume"].mean())
    last = float(df["close"].iloc[-1])
    problems = []
    if turnover_cr < lq.min_avg_turnover_cr:
        problems.append(f"turnover ₹{turnover_cr:.2f} Cr < ₹{lq.min_avg_turnover_cr:g} Cr")
    if avg_vol < lq.min_avg_volume:
        problems.append(f"volume {avg_vol:,.0f} < {lq.min_avg_volume:,.0f}")
    if last < lq.min_price:
        problems.append(f"price ₹{last:.2f} < ₹{lq.min_price:g}")
    details = {"avg_turnover_cr": round(turnover_cr, 2), "avg_volume": round(avg_vol), "last_close": last}
    if problems:
        return CheckResult("liquidity", False, "; ".join(problems), details)
    return CheckResult("liquidity", True, f"{lq.avg_days}d avg turnover ₹{turnover_cr:.1f} Cr, volume {avg_vol:,.0f}", details)


def weekly_check(df: pd.DataFrame, cfg) -> CheckResult:
    w = cfg.screener.weekly
    wk = resample_weekly(df)
    if len(wk) < w.lookback_weeks + 1:
        return CheckResult("weekly", False, f"only {len(wk)} weekly bars")
    window = wk.iloc[-w.lookback_weeks :]
    peak_label = window["high"].idxmax()
    peak_pos = wk.index.get_loc(peak_label)
    peak = float(wk["high"].iloc[peak_pos])
    weeks_since = len(wk) - 1 - peak_pos
    if weeks_since < w.min_weeks_since_peak:
        return CheckResult("weekly", False, f"peak ₹{peak:.2f} is in the current week: no pullback yet", {"peak_date": peak_label})
    pre = wk.iloc[max(0, peak_pos - w.advance_lookback_weeks) : peak_pos + 1]
    adv_low = float(pre["low"].min())
    adv_low_date = pre["low"].idxmin()
    advance = (peak / adv_low - 1) * 100
    post = wk.iloc[peak_pos + 1 :]
    pull_low = float(post["low"].min())
    pullback = (1 - pull_low / peak) * 100
    details = {
        "advance_pct": round(advance, 1),
        "pullback_pct": round(pullback, 1),
        "advance_start": adv_low_date,
        "peak_date": peak_label,
        "peak": peak,
        "pullback_low": pull_low,
        "pullback_low_date": post["low"].idxmin(),
        "weeks_since_peak": weeks_since,
    }
    text = f"advance {advance:+.1f}% ({adv_low_date:%d-%b} → {peak_label:%d-%b}), pullback −{pullback:.1f}% over {weeks_since} wk"
    if advance < w.min_advance_pct:
        return CheckResult("weekly", False, f"advance {advance:.1f}% < {w.min_advance_pct:g}% ({text})", details)
    if pullback < w.min_pullback_pct:
        return CheckResult("weekly", False, f"pullback {pullback:.1f}% < {w.min_pullback_pct:g}% ({text})", details)
    if w.max_pullback_of_advance_pct is not None:
        retrace = (peak - pull_low) / (peak - adv_low) * 100
        if retrace > w.max_pullback_of_advance_pct:
            return CheckResult("weekly", False, f"retraced {retrace:.0f}% of the advance (> {w.max_pullback_of_advance_pct:g}%)", details)
    return CheckResult("weekly", True, text, details)


def find_consolidation(df: pd.DataFrame, cfg) -> Consolidation:
    d = cfg.screener.daily
    window = df.iloc[-d.consolidation_lookback_days :]
    high_label = window["high"].idxmax()
    pos = df.index.get_loc(high_label)
    return Consolidation(pos, high_label, float(df["high"].iloc[pos]), len(df) - 1 - pos)


def higher_lows_check(df: pd.DataFrame, base: Consolidation, cfg) -> CheckResult:
    d = cfg.screener.daily
    if base.base_days < d.min_base_days:
        return CheckResult("7a higher lows", False, f"consolidation high only {base.base_days} bars ago (< {d.min_base_days})")
    lows = [s for s in swing_points(df, d.swing_lookaround, "low", d.use_provisional_last_low) if s.pos > base.high_pos]
    if len(lows) < 2:
        return CheckResult("7a higher lows", False, f"{len(lows)} swing low(s) since the {base.high_date:%d-%b} high: structure not formed yet")
    prior, latest = lows[-2], lows[-1]
    details = {"prior_low": prior.price, "prior_date": prior.date, "latest_low": latest.price, "latest_date": latest.date, "swings": lows}
    if higher_lows(prior.price, latest.price):
        return CheckResult("7a higher lows", True, f"₹{prior.price:.2f} ({prior.date:%d-%b}) → ₹{latest.price:.2f} ({latest.date:%d-%b})", details)
    return CheckResult("7a higher lows", False, f"lower low: ₹{prior.price:.2f} ({prior.date:%d-%b}) → ₹{latest.price:.2f} ({latest.date:%d-%b})", details)


def contraction_check(df: pd.DataFrame, base: Consolidation, cfg) -> CheckResult:
    c = cfg.screener.daily.contraction
    if c.method == "swing_amplitude":
        return _swing_amplitude(df, base, cfg)
    trp = true_range_pct(df)
    recent = trp.iloc[-c.recent_days :]
    earlier = trp.iloc[base.high_pos : len(df) - c.recent_days]
    source = "earlier part of the base"
    if len(earlier) < c.min_earlier_days:
        earlier = trp.iloc[max(0, len(df) - c.recent_days - c.fallback_earlier_days) : len(df) - c.recent_days]
        source = f"{c.fallback_earlier_days} bars before"
    ratio = float(recent.mean() / earlier.mean()) if earlier.mean() else float("inf")
    details = {"recent_range_pct": round(float(recent.mean()), 2), "earlier_range_pct": round(float(earlier.mean()), 2), "ratio": round(ratio, 2)}
    text = f"avg range {recent.mean():.2f}% (last {c.recent_days}d) vs {earlier.mean():.2f}% ({source}), ratio {ratio:.2f}"
    return CheckResult("7b contraction", ratio <= c.max_ratio, text + (f" ≤ {c.max_ratio}" if ratio <= c.max_ratio else f" > {c.max_ratio}"), details)


def _swing_amplitude(df: pd.DataFrame, base: Consolidation, cfg) -> CheckResult:
    k = cfg.screener.daily.swing_lookaround
    highs = [s for s in swing_points(df, k, "high") if s.pos >= base.high_pos]
    lows = [s for s in swing_points(df, k, "low") if s.pos > base.high_pos]
    amps = []
    for h in highs:
        nxt = next((lo for lo in lows if lo.pos > h.pos), None)
        if nxt:
            amps.append((h.price - nxt.price) / h.price * 100)
    if len(amps) < 2:
        return CheckResult("7b contraction", False, f"{len(amps)} pullback(s) in the base: cannot compare swings")
    ok = amps[-1] < amps[-2]
    return CheckResult("7b contraction", ok, f"pullback depth {amps[-2]:.1f}% → {amps[-1]:.1f}%", {"amplitudes": amps})


def ema_reclaim_check(df: pd.DataFrame, cfg) -> CheckResult:
    r = cfg.screener.daily.ema_reclaim
    close = df["close"]
    parts, passed_any, details = [], False, {}
    for period in r.emas:
        e = ema(close, period)
        above = close.iloc[-1] > e.iloc[-1]
        look_c = close.iloc[-r.reclaim_lookback_days - 1 : -1]
        look_e = e.iloc[-r.reclaim_lookback_days - 1 : -1]
        dipped = bool((look_c < look_e).any())
        slope = float(slope_pct_per_day(e, r.slope_days).iloc[-1])
        slope_ok = slope >= r.min_slope_pct_per_day
        reclaim_date = None
        cross = (close > e) & (close.shift(1) <= e.shift(1))
        if cross.any():
            reclaim_date = cross[cross].index[-1]
        ok = above and slope_ok and (dipped or not r.require_prior_close_below)
        passed_any |= ok
        details[f"ema{period}"] = {"value": float(e.iloc[-1]), "above": above, "dipped": dipped, "slope": slope, "reclaim_date": reclaim_date, "ok": ok}
        state = "above" if above else "below"
        if above and r.require_prior_close_below and not dipped:
            state = f"above, no close below in {r.reclaim_lookback_days}d (not a reclaim)"
        elif above and reclaim_date is not None:
            state = f"reclaimed {reclaim_date:%d-%b}"
        parts.append(f"EMA{period} ₹{e.iloc[-1]:.2f} {state}, slope {slope:+.2f}%/d{'' if slope_ok else ' (falling)'}")
    return CheckResult("7c EMA reclaim", passed_any, "; ".join(parts), details)


def disqualify_check(df: pd.DataFrame, cfg) -> CheckResult:
    """Stage 1.8. ``passed=True`` means the stock IS disqualified."""
    dq = cfg.screener.disqualify
    k = cfg.screener.daily.swing_lookaround
    highs, lows = swing_points(df, k, "high"), swing_points(df, k, "low")
    n = dq.swing_count
    if len(highs) < n or len(lows) < n:
        return CheckResult("8 disqualify", False, "not enough swings to judge the trend")
    falling_highs = all(highs[-i].price < highs[-i - 1].price for i in range(1, n))
    falling_lows = all(lows[-i].price < lows[-i - 1].price for i in range(1, n))
    below_all, notes = True, []
    for period in dq.emas:
        e = ema(df["close"], period)
        below = bool((df["close"].iloc[-dq.below_days :] < e.iloc[-dq.below_days :]).all())
        declining = float(slope_pct_per_day(e, 5).iloc[-1]) < 0
        below_all &= below and declining
        notes.append(f"EMA{period} {'declining' if declining else 'not declining'}, {'below' if below else 'not below'} {dq.below_days}d")
    disq = falling_highs and falling_lows and below_all
    text = f"highs {'falling' if falling_highs else 'not falling'}, lows {'falling' if falling_lows else 'not falling'}; " + "; ".join(notes)
    return CheckResult("8 disqualify", disq, ("DISQUALIFIED: " if disq else "") + text)


# ------------------------------------------------------------------ the screener
class Screener:
    def __init__(self, settings: "Settings", reference: "ReferenceData | None" = None, ticks: TickTable | None = None):
        self.s = settings
        self.ref = reference
        self.ticks = ticks or TickTable.from_settings(settings)

    def run(self, data: dict[str, pd.DataFrame], quotes: "dict[str, Quote] | None" = None) -> ScreenResult:
        cfg = self.s
        win = cfg.screener.return_windows
        lengths = {"m1": win.m1, "m3": win.m3, "m6": win.m6}
        rows: dict[str, ScreenRow] = {}
        returns_tbl: dict[str, dict[str, float]] = {}
        as_of = None
        for sym, df in data.items():
            info = self.ref.get(sym) if self.ref else None
            row = ScreenRow(sym, name=(info.name if info else sym), industry=(info.industry if info else None))
            row.groups = info.groups if info else []
            rows[sym] = row
            if df is None or len(df) < cfg.data.min_history_trading_days:
                row.skipped_reason = f"only {0 if df is None else len(df)} daily bars (< {cfg.data.min_history_trading_days})"
                continue
            as_of = max(as_of, df.index[-1]) if as_of is not None else df.index[-1]
            closes = df["close"]
            row.last_close = float(closes.iloc[-1])
            row.returns = {
                w: (float(closes.iloc[-1] / closes.iloc[-1 - n] - 1) * 100 if len(closes) > n else float("nan")) for w, n in lengths.items()
            }
            returns_tbl[sym] = row.returns

        # step 1: rank the universe separately on each window
        ret_df = pd.DataFrame(returns_tbl).T if returns_tbl else pd.DataFrame(columns=list(WINDOWS))
        top_pct = cfg.screener.momentum.top_rank_pct / 100.0
        for w in WINDOWS:
            col = ret_df[w].dropna() if w in ret_df else pd.Series(dtype=float)
            if col.empty:
                continue
            ranks = col.rank(ascending=False, method="min")
            cutoff = math.ceil(top_pct * len(col))
            for sym, rk in ranks.items():
                rows[sym].rank_pct[w] = float((rk - 1) / len(col) * 100)
                rows[sym].top[w] = bool(rk <= cutoff)

        # steps 2–4 and 6–8 per stock
        for sym, row in rows.items():
            if row.skipped_reason:
                continue
            df = data[sym]
            row.momentum, row.both_conditions = momentum_check(row.returns, row.top, cfg)
            row.liquidity = liquidity_check(df, cfg)
            row.weekly = weekly_check(df, cfg)
            base = find_consolidation(df, cfg)
            row.consolidation = base
            row.higher_lows = higher_lows_check(df, base, cfg)
            row.contraction = contraction_check(df, base, cfg)
            row.ema_reclaim = ema_reclaim_check(df, cfg)
            row.disqualified = disqualify_check(df, cfg)
            self._flags(row, df, (quotes or {}).get(sym))

        # step 5: hot themes (groups with ≥ N momentum passers)
        group_members: dict[str, set[str]] = {}
        if self.ref is not None:
            group_members = self.ref.groups(cfg.screener.themes.use_industry)
        hot: dict[str, list[str]] = {}
        for g, members in group_members.items():
            passers = sorted(m for m in members if m in rows and rows[m].momentum and rows[m].momentum.passed)
            if len(passers) >= cfg.screener.themes.min_members_hot:
                hot[g] = passers
        for row in rows.values():
            row.hot_groups = sorted(g for g in hot if row.symbol in group_members.get(g, set()))

        ordered = sorted(rows.values(), key=self._sort_key)
        return ScreenResult(ordered, hot, as_of)

    def _sort_key(self, row: ScreenRow):
        keys = []
        for k in self.s.screener.rank_order:
            if k == "all_checks_pass":
                keys.append(0 if row.all_checks_pass else 1)
            elif k == "in_hot_group":
                keys.append(0 if row.hot_groups else 1)
            elif k == "both_conditions":
                keys.append(0 if row.both_conditions else 1)
            elif k == "fastest":
                keys.append(-row.fastest(self.s.screener.themes.fastest_metric))
        keys.append(0 if row.on_watchlist else 1)
        return (*keys, row.symbol)

    def _flags(self, row: ScreenRow, df: pd.DataFrame, quote: "Quote | None") -> None:
        sv = self.s.surveillance
        info = self.ref.get(row.symbol) if self.ref else None
        if info:
            row.flags.extend(info.surveillance_flags)
            if sv.exclude_asm and info.asm:
                row.excluded_reason = f"{info.asm} (excluded by settings)"
            if sv.exclude_gsm and info.gsm:
                row.excluded_reason = f"{info.gsm} (excluded by settings)"
            if sv.exclude_t2t and info.t2t:
                row.excluded_reason = f"trade-for-trade {info.series} (excluded by settings)"
        if len(df) >= 2 and self.ref is not None and info is not None:
            prev_close, last = float(df["close"].iloc[-2]), float(df["close"].iloc[-1])
            tick = self.ref.tick(row.symbol, prev_close, self.ticks, self.s)
            limits = self.ref.circuit_limits(row.symbol, prev_close, tick, self.s)
            for f in limits.proximity(last, self.s.price_bands.near_circuit_pct):
                row.flags.append(f"{f} (last close vs {limits.source} limits ₹{limits.lower}–₹{limits.upper})")
        if quote is not None and self.ref is not None and info is not None and quote.upper_circuit and quote.lower_circuit:
            tick = self.ref.tick(row.symbol, quote.prev_close or quote.ltp, self.ticks, self.s)
            limits = self.ref.circuit_limits(row.symbol, quote.prev_close or quote.ltp, tick, self.s, quote=quote)
            for f in limits.proximity(quote.ltp, self.s.price_bands.near_circuit_pct):
                row.flags.append(f"{f} (live)")


__all__ = [
    "CheckResult",
    "Consolidation",
    "ScreenRow",
    "ScreenResult",
    "Screener",
    "higher_lows",
    "weekly_check",
    "higher_lows_check",
    "contraction_check",
    "ema_reclaim_check",
    "disqualify_check",
    "find_consolidation",
    "momentum_check",
    "liquidity_check",
]

