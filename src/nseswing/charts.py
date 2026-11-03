"""Annotated weekly, daily and intraday charts for chart review (Plotly).

Visual encoding (validated for colour-vision deficiency, light and dark):

* candles are monochrome: hollow = up, filled = down, so colour stays free for levels
* 9 EMA blue, 21 EMA orange, default AVWAP aqua (the three all-pairs-safe slots);
  the 50 EMA and manual AVWAPs recede to muted grey; every line has a legend entry
  and, where they do not collide, a direct label at the right edge
* support zones are grey washes, darker = higher priority, each with a text label;
  broken zones are faint and say "broken"
* plan lines use status colour with a text label: stop = critical, targets = good,
  entry = primary ink; circuit limits are dashed threshold lines
* volume sits in its own row (never a second y-axis)

Every figure has a table twin (see :func:`levels_table`), so no value is colour-only.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from nseswing.indicators import avwap_intraday, resample_weekly
from nseswing.intraday.bars import resample_bars

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.data.reference import CircuitLimits
    from nseswing.intraday.trigger import TriggerEvent
    from nseswing.levels import LevelMap, Zone
    from nseswing.orders.base import Fill
    from nseswing.risk import TradePlan
    from nseswing.screener import ScreenRow

FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"


@dataclass(frozen=True)
class Theme:
    surface: str
    ink: str
    ink2: str
    muted: str
    grid: str
    axis: str
    ema9: str
    ema21: str
    avwap: str
    other: str
    candle: str
    zone_high: str
    zone_medium: str
    zone_low: str
    zone_broken: str
    gap: str
    stop: str
    target: str
    entry: str
    volume: str


THEMES = {
    "light": Theme(
        "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7",
        "#2a78d6", "#eb6834", "#1baf7a", "#898781", "#52514e",
        "rgba(82,81,78,0.20)", "rgba(137,135,129,0.15)", "rgba(137,135,129,0.08)", "rgba(137,135,129,0.05)", "rgba(137,135,129,0.12)",
        "#d03b3b", "#0ca30c", "#0b0b0b", "#c3c2b7",
    ),
    "dark": Theme(
        "#1a1a19", "#f0efec", "#c3c2b7", "#898781", "#2c2c2a", "#383835",
        "#3987e5", "#d95926", "#199e70", "#898781", "#c3c2b7",
        "rgba(195,194,183,0.20)", "rgba(195,194,183,0.13)", "rgba(195,194,183,0.07)", "rgba(137,135,129,0.05)", "rgba(137,135,129,0.14)",
        "#d03b3b", "#0ca30c", "#f0efec", "#383835",
    ),
}


def _theme(theme: str | Theme) -> Theme:
    return theme if isinstance(theme, Theme) else THEMES[theme]


def _base(rows_heights=(0.8, 0.2)) -> go.Figure:
    return make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=list(rows_heights), vertical_spacing=0.03)


def _style(fig: go.Figure, t: Theme, title: str, subtitle: str, height: int) -> go.Figure:
    fig.update_layout(
        template="simple_white",
        paper_bgcolor=t.surface,
        plot_bgcolor=t.surface,
        font=dict(family=FONT, color=t.ink2, size=12),
        title=dict(text=f"{html.escape(title)}<br><span style='font-size:12px;color:{t.ink2}'>{html.escape(subtitle)}</span>", font=dict(color=t.ink, size=16), x=0.01, xanchor="left"),
        legend=dict(orientation="h", yanchor="top", y=-0.07, xanchor="left", x=0, font=dict(color=t.ink2, size=11), bgcolor="rgba(0,0,0,0)"),
        margin=dict(l=56, r=170, t=80, b=90),
        hovermode="x unified",
        hoverlabel=dict(font=dict(family=FONT)),
        height=height,
        showlegend=True,
    )
    fig.update_xaxes(showgrid=False, linecolor=t.axis, tickfont=dict(color=t.muted), rangeslider_visible=False, ticks="")
    fig.update_yaxes(showgrid=True, gridcolor=t.grid, gridwidth=1, zeroline=False, linecolor=t.axis, tickfont=dict(color=t.muted), ticks="", side="left")
    fig.update_yaxes(showgrid=False, row=2, col=1, showticklabels=False)
    return fig


def _candles(fig: go.Figure, x, df: pd.DataFrame, t: Theme, name: str = "Price") -> None:
    fig.add_trace(
        go.Candlestick(
            x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"], name=name, showlegend=False,
            increasing=dict(line=dict(color=t.candle, width=1), fillcolor=t.surface),
            decreasing=dict(line=dict(color=t.candle, width=1), fillcolor=t.candle),
            whiskerwidth=0,
        ),
        row=1, col=1,
    )
    fig.add_trace(go.Bar(x=x, y=df["volume"], marker=dict(color=t.volume, line=dict(width=0)), name="Volume", showlegend=False, hovertemplate="vol %{y:,.0f}<extra></extra>"), row=2, col=1)


def _line(fig, x, y, name, color, width=2.0, dash="solid") -> None:
    fig.add_trace(go.Scatter(x=x, y=y, mode="lines", name=name, line=dict(color=color, width=width, dash=dash, shape="linear"), hovertemplate=f"{name} ₹%{{y:.2f}}<extra></extra>"), row=1, col=1)


class _Labels:
    """Right-margin labels placed after the y-range is known: plan lines first, then lines.

    Labels closer than ``gap`` (fraction of the y-range) to a placed one are merged
    (plan labels) or dropped (EMA / AVWAP labels: the legend and hover still carry them).
    """

    def __init__(self) -> None:
        self.items: list[tuple[int, float, str]] = []

    def add(self, y: float, text: str, priority: int) -> None:
        if y == y:  # skip NaN
            self.items.append((priority, float(y), text))

    def place(self, fig, lo: float, hi: float, t: "Theme", gap: float = 0.035) -> None:
        span = max(hi - lo, 1e-9)
        placed: list[list] = []  # [y, text, priority]

        def lines(text: str) -> int:
            return text.count("<br>") + 1

        for prio, y, text in sorted(self.items, key=lambda v: (v[0], -v[1])):
            if not lo <= y <= hi:
                continue
            clash = next((p for p in placed if abs(p[0] - y) / span < gap * (lines(p[1]) + 1) / 2), None)
            if clash is None:
                placed.append([y, text, prio])
            elif prio == 0 and clash[2] == 0:
                clash[1] = f"{clash[1]}<br>{text}"
        for y, text, _ in placed:
            fig.add_annotation(xref="x domain", x=1.0, y=y, xanchor="left", align="left", xshift=6, showarrow=False, text=text, font=dict(color=t.ink2, size=11), row=1, col=1)


def _zone_fill(z: "Zone", t: Theme) -> str:
    if z.broken:
        return t.zone_broken
    return {"high": t.zone_high, "medium": t.zone_medium, "low": t.zone_low}[z.priority]


def _zones(fig, zones: Iterable["Zone"], t: Theme, lo: float | None = None, hi: float | None = None, gap: float = 0.04, side: str = "left") -> None:
    span = (hi - lo) if (lo is not None and hi is not None) else None
    placed: list[float] = []
    ordered = sorted(zones, key=lambda z: ({"high": 0, "medium": 1, "low": 2}[z.priority], z.broken, -float(z.top)))
    for z in zones:
        fig.add_hrect(y0=float(z.bottom), y1=float(z.top), fillcolor=_zone_fill(z, t), line_width=0, layer="below", row=1, col=1)
    for z in ordered:
        y = float(z.top)
        if span is not None and (not lo <= y <= hi or any(abs(p - y) / span < gap for p in placed)):
            continue
        placed.append(y)
        kinds = " + ".join(sorted({lv.label.split(" ₹")[0].split(" (")[0] for lv in z.levels}))
        tag = " · FIRST RETEST" if z.first_retest else ""
        state = f" · broken {z.broken_on}" if z.broken else ""
        fig.add_annotation(
            xref="x domain", x=0.005 if side == "left" else 0.995, y=y, yanchor="bottom", xanchor=side, showarrow=False,
            text=f"{z.priority.upper()} zone ₹{z.bottom}–₹{z.top} · {html.escape(kinds)}{tag}{state}",
            font=dict(color=t.ink2, size=10), bgcolor=_alpha(t.surface, 0.8), row=1, col=1,
        )


def _alpha(hex_color: str, a: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{a})"


def _plan_lines(fig, plan: "TradePlan | None", t: Theme, labels: _Labels, x0=None, x1=None) -> list[float]:
    if plan is None:
        return []
    ys = []
    rows = [("Entry", plan.entry, t.entry, 1.2, ""), ("Stop", plan.stop, t.stop, 1.8, f" (−{plan.stop_pct}%)")]
    for k in ("3R", "5R"):
        if k in plan.targets:
            rows.append((k, plan.targets[k], t.target, 1.8, ""))
    for label, price, color, width, suffix in rows:
        y = float(price)
        ys.append(y)
        if x0 is None:
            fig.add_hline(y=y, line=dict(color=color, width=width), row=1, col=1)
        else:
            fig.add_shape(type="line", x0=x0, x1=x1, y0=y, y1=y, line=dict(color=color, width=width), row=1, col=1)
        labels.add(y, f"{label} ₹{price}{suffix}", 0)
    return ys


def _circuits(fig, circuit: "CircuitLimits | None", lo: float, hi: float, t: Theme, labels: _Labels) -> str:
    if circuit is None:
        return ""
    for name, v in (("Upper circuit", float(circuit.upper)), ("Lower circuit", float(circuit.lower))):
        if lo <= v <= hi:
            fig.add_hline(y=v, line=dict(color=t.muted, width=1, dash="dash"), row=1, col=1)
            labels.add(v, f"{name} ₹{v:.2f}", 1)
    return f"circuits ₹{circuit.lower}–₹{circuit.upper} ({circuit.source})"


def _yrange(fig, lows: Iterable[float], highs: Iterable[float], extra: Iterable[float] = ()) -> tuple[float, float]:
    vals_lo = [v for v in lows] + [v for v in extra]
    vals_hi = [v for v in highs] + [v for v in extra]
    lo, hi = min(vals_lo), max(vals_hi)
    pad = (hi - lo) * 0.04
    fig.update_yaxes(range=[lo - pad, hi + pad], row=1, col=1)
    return lo - pad, hi + pad


# ------------------------------------------------------------------ daily
def daily_chart(
    symbol: str,
    daily: pd.DataFrame,
    level_map: "LevelMap",
    row: "ScreenRow | None" = None,
    plan: "TradePlan | None" = None,
    circuit: "CircuitLimits | None" = None,
    theme: str | Theme = "light",
    bars: int = 120,
    height: int = 760,
) -> go.Figure:
    t = _theme(theme)
    df = daily.iloc[-bars:]
    x = list(df.index.strftime("%d-%b-%y"))
    fig = _base()
    _candles(fig, x, df, t)
    labels = _Labels()
    for p, series in level_map.emas.items():
        color = t.ema9 if p == 9 else t.ema21 if p == 21 else t.other
        s = series.reindex(df.index)
        _line(fig, x, s, f"EMA{p}", color, 2.0 if p in (9, 21) else 1.5)
        labels.add(float(s.iloc[-1]), f"EMA{p}", 2)
    for a in level_map.avwaps:
        s = a.series.reindex(df.index)
        color = t.avwap if a.source == "default" else t.other
        _line(fig, x, s, a.label, color, 2.0, "solid" if a.source == "default" else "dot")
        if pd.notna(s.iloc[-1]):
            labels.add(float(s.iloc[-1]), "AVWAP" if a.source == "default" else "AVWAP (manual)", 2)
        if a.anchor_date in df.index:
            ax = a.anchor_date.strftime("%d-%b-%y")
            fig.add_trace(go.Scatter(x=[ax], y=[float(df.loc[a.anchor_date, "high"])], mode="markers", marker=dict(symbol="diamond", size=10, color=t.avwap if a.source == "default" else t.other, line=dict(color=t.surface, width=2)), name="AVWAP anchor", hovertemplate="AVWAP anchor ₹%{y:.2f}<extra></extra>"), row=1, col=1)
    in_view = set(df.index)
    sh = [s for s in level_map.swing_highs if s.date in in_view]
    sl = [s for s in level_map.swing_lows if s.date in in_view]
    if sh:
        fig.add_trace(go.Scatter(x=[s.date.strftime("%d-%b-%y") for s in sh], y=[s.price * 1.006 for s in sh], mode="markers", marker=dict(symbol="triangle-down", size=8, color=t.muted), name="Swing highs", customdata=[s.price for s in sh], hovertemplate="swing high ₹%{customdata:.2f}<extra></extra>"), row=1, col=1)
    if sl:
        fig.add_trace(go.Scatter(x=[s.date.strftime("%d-%b-%y") for s in sl], y=[s.price * 0.994 for s in sl], mode="markers", marker=dict(symbol="triangle-up", size=8, color=t.muted), name="Swing lows", customdata=[s.price for s in sl], hovertemplate="swing low ₹%{customdata:.2f}<extra></extra>"), row=1, col=1)
    for g in level_map.gaps:
        if g.direction == "up" and not g.filled and g.date in in_view:
            fig.add_shape(type="rect", x0=g.date.strftime("%d-%b-%y"), x1=x[-1], y0=g.bottom, y1=g.top, fillcolor=t.gap, line=dict(width=0), layer="below", row=1, col=1)
            fig.add_annotation(x=g.date.strftime("%d-%b-%y"), y=g.top, text="unfilled gap", showarrow=False, yanchor="bottom", xanchor="left", font=dict(color=t.muted, size=10), row=1, col=1)
    if row is not None and row.higher_lows is not None and "prior_date" in row.higher_lows.details:
        d = row.higher_lows.details
        if d["prior_date"] in in_view and d["latest_date"] in in_view:
            ok = row.higher_lows.passed
            fig.add_trace(go.Scatter(x=[d["prior_date"].strftime("%d-%b-%y"), d["latest_date"].strftime("%d-%b-%y")], y=[d["prior_low"], d["latest_low"]], mode="lines", line=dict(color=t.ink2, width=1), name="Higher low (7a)" if ok else "Lower low (7a)", hoverinfo="skip"), row=1, col=1)
            fig.add_annotation(x=d["latest_date"].strftime("%d-%b-%y"), y=d["latest_low"], text="higher low ✓" if ok else "lower low ✗", showarrow=False, yshift=-14, font=dict(color=t.ink2, size=10), bgcolor=_alpha(t.surface, 0.8), row=1, col=1)
    recent = x[max(0, len(x) - 25)]
    extra = _plan_lines(fig, plan, t, labels, x0=recent, x1=x[-1])
    near = [float(z.top) for z in level_map.zones] + [float(z.bottom) for z in level_map.zones]
    lo, hi = _yrange(fig, df["low"], df["high"], extra + near)
    _zones(fig, level_map.zones, t, lo, hi)
    circ = _circuits(fig, circuit, lo, hi, t, labels)
    labels.place(fig, lo, hi, t)
    fig.update_xaxes(type="category", nticks=10)
    return _style(fig, t, f"{symbol} · daily · to {df.index[-1]:%d %b %Y}", _subtitle(row, circ), height)


def _subtitle(row: "ScreenRow | None", circ: str) -> str:
    parts = []
    if row is not None:
        parts.append("Stage 1: all checks pass" if row.all_checks_pass else "Stage 1: NOT all checks pass")
        if row.hot_groups:
            parts.append("hot: " + ", ".join(g.split(": ", 1)[-1] for g in row.hot_groups))
        if row.flags:
            parts.append("flags: " + "; ".join(row.flags))
    if circ:
        parts.append(circ)
    return " · ".join(parts) or " "


# ------------------------------------------------------------------ weekly
def weekly_chart(
    symbol: str,
    daily: pd.DataFrame,
    level_map: "LevelMap",
    row: "ScreenRow | None" = None,
    plan: "TradePlan | None" = None,
    theme: str | Theme = "light",
    weeks: int = 78,
    height: int = 620,
) -> go.Figure:
    t = _theme(theme)
    wk = resample_weekly(daily).iloc[-weeks:]
    x = list(wk.index.strftime("%d-%b-%y"))
    fig = _base()
    _candles(fig, x, wk, t, name="Weekly")
    labels = _Labels()
    if row is not None and row.weekly is not None and "peak_date" in row.weekly.details:
        d = row.weekly.details
        pts = [(d.get("advance_start"), wk["low"]), (d.get("peak_date"), wk["high"]), (d.get("pullback_low_date"), wk["low"])]
        xs, ys = [], []
        for when, col in pts:
            if when is not None and when in wk.index:
                xs.append(when.strftime("%d-%b-%y"))
                ys.append(float(col.loc[when]))
        if len(xs) == 3:
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers", line=dict(color=t.ink2, width=1), marker=dict(size=8, color=t.ink2, line=dict(color=t.surface, width=2)), name="Advance → pullback (weekly check)", hoverinfo="skip"), row=1, col=1)
            fig.add_annotation(x=xs[1], y=ys[1], text=f"advance +{d['advance_pct']}%", showarrow=False, yshift=12, font=dict(color=t.ink2, size=11), row=1, col=1)
            fig.add_annotation(x=xs[2], y=ys[2], text=f"pullback −{d['pullback_pct']}%", showarrow=False, yshift=-14, font=dict(color=t.ink2, size=11), row=1, col=1)
    zones = [z for z in level_map.zones if not z.broken][:2]
    extra = _plan_lines(fig, plan, t, labels, x0=x[max(0, len(x) - 8)], x1=x[-1])
    lo, hi = _yrange(fig, wk["low"], wk["high"], extra)
    _zones(fig, zones, t, lo, hi)
    labels.place(fig, lo, hi, t)
    fig.update_xaxes(type="category", nticks=10)
    status = row.weekly.short() if (row is not None and row.weekly is not None) else " "
    return _style(fig, t, f"{symbol} · weekly", status, height)


# ------------------------------------------------------------------ intraday
def intraday_chart(
    symbol: str,
    intraday_1m: pd.DataFrame,
    timeframe: int,
    level_map: "LevelMap",
    daily_before: pd.DataFrame,
    plan: "TradePlan | None" = None,
    fills: "list[Fill] | None" = None,
    trigger_events: "list[TriggerEvent] | None" = None,
    circuit: "CircuitLimits | None" = None,
    theme: str | Theme = "light",
    upto: pd.Timestamp | None = None,
    height: int = 700,
) -> go.Figure:
    """``upto`` cuts the chart at a replay step (later bars are not drawn)."""
    t = _theme(theme)
    one = intraday_1m if upto is None else intraday_1m[intraday_1m.index <= upto]
    bars = resample_bars(one, timeframe)
    x = list(bars.index.tz_localize(None))
    fig = _base((0.82, 0.18))
    _candles(fig, x, bars, t)
    labels = _Labels()
    lm = level_map
    if lm.default_avwap is not None and not one.empty:
        av = avwap_intraday(daily_before, lm.default_avwap.anchor_pos, one)
        av = av.groupby(av.index.floor(f"{timeframe}min")).last().reindex(bars.index) if timeframe > 1 else av.reindex(bars.index)
        _line(fig, x, av, "AVWAP (daily anchor)", t.avwap)
        labels.add(float(av.dropna().iloc[-1]), "AVWAP", 2)
    for p, series in lm.emas.items():
        v = float(series.iloc[-1])
        color = t.ema9 if p == 9 else t.ema21 if p == 21 else t.other
        _line(fig, [x[0], x[-1]], [v, v], f"EMA{p} (prior close)", color, 1.6 if p in (9, 21) else 1.2)
        labels.add(v, f"EMA{p}", 2)
    extra: list[float] = []
    if plan is not None and plan.created_at is not None and (upto is None or pd.Timestamp(plan.created_at) <= upto):
        x0 = pd.Timestamp(plan.created_at).tz_convert("Asia/Kolkata").tz_localize(None)
        extra = _plan_lines(fig, plan, t, labels, x0=x0, x1=x[-1])
    names = {"dip": "Dip into zone", "trigger": "Trigger", "zone_broken": "Zone broken", "invalid_trigger": "Invalid trigger"}
    for ev in trigger_events or []:
        if ev.price is None or (upto is not None and ev.ts > upto) or ev.kind not in names:
            continue
        sym = {"dip": "triangle-up", "trigger": "diamond", "zone_broken": "x", "invalid_trigger": "x"}[ev.kind]
        fig.add_trace(go.Scatter(x=[ev.ts.tz_convert("Asia/Kolkata").tz_localize(None)], y=[ev.price], mode="markers", marker=dict(symbol=sym, size=11, color=t.ink if ev.kind == "trigger" else t.muted, line=dict(color=t.surface, width=2)), name=names[ev.kind], hovertemplate=f"{names[ev.kind]} ₹%{{y:.2f}}<extra></extra>"), row=1, col=1)
    session_day = bars.index[0].date() if len(bars) else None
    for f in fills or []:
        if (upto is not None and f.ts > upto) or f.ts.date() != session_day:
            continue
        color = t.entry if f.side.value == "BUY" else (t.stop if f.purpose == "stop" else t.target)
        name = "Entry fill" if f.side.value == "BUY" else {"stop": "Stop fill", "target": "Partial (target)"}.get(f.purpose, "Exit")
        fig.add_trace(go.Scatter(x=[f.ts.tz_convert("Asia/Kolkata").tz_localize(None)], y=[float(f.price)], mode="markers", marker=dict(symbol="triangle-up" if f.side.value == "BUY" else "triangle-down", size=12, color=color, line=dict(color=t.surface, width=2)), name=name, hovertemplate=f"{name} {f.qty} @ ₹%{{y:.2f}}<extra></extra>"), row=1, col=1)
    if not one.empty:
        open_px = float(one["open"].iloc[0])
        fig.add_annotation(x=x[0], y=open_px, text=f"open ₹{open_px:.2f} ({(open_px / lm.last_close - 1) * 100:+.2f}% vs prior close)", showarrow=False, xanchor="left", yanchor="bottom", yshift=8, font=dict(color=t.ink2, size=10), bgcolor=_alpha(t.surface, 0.8), row=1, col=1)
    win_lo, win_hi = float(bars["low"].min()) * 0.985, float(bars["high"].max()) * 1.015
    near = [v for z in lm.zones if float(z.top) >= win_lo and float(z.bottom) <= win_hi for v in (float(z.bottom), float(z.top))]
    lo, hi = _yrange(fig, bars["low"], bars["high"], extra + near)
    _zones(fig, lm.zones, t, lo, hi, side="right")
    circ = _circuits(fig, circuit, lo, hi, t, labels)
    labels.place(fig, lo, hi, t, gap=0.03)
    day = bars.index[0].strftime("%d %b %Y") if len(bars) else ""
    sub = f"{timeframe}-min bars · IST · zones, AVWAP and EMAs as marked before the open" + (f" · {circ}" if circ else "")
    return _style(fig, t, f"{symbol} · intraday {day}", sub, height)


# ------------------------------------------------------------------ tables (the chart's table twin) and report
def levels_table(level_map: "LevelMap") -> pd.DataFrame:
    rows = []
    for z in level_map.zones:
        for lv in z.levels:
            rows.append({"zone": f"{z.priority.upper()} ₹{z.bottom}–₹{z.top}", "first_retest": z.first_retest, "broken": z.broken, "level": lv.label, "kind": lv.kind, "price": round(lv.price, 2)})
    return pd.DataFrame(rows)


def _plan_html(plan: "TradePlan") -> str:
    from nseswing.plancard import _rows

    body = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in _rows(plan))
    notes = "".join(f"<li>⚠ {html.escape(w)}</li>" for w in plan.warnings) + "".join(f"<li>ℹ {html.escape(i)}</li>" for i in plan.info)
    return f"<h2>Trade plan · {html.escape(plan.symbol)}{' (draft)' if plan.is_draft else ''}</h2><table class='kv'>{body}</table><ul>{notes}</ul>"


def write_report(path: Path, title: str, figures: list[go.Figure], plan: "TradePlan | None" = None, tables: list[tuple[str, pd.DataFrame]] | None = None, plotly_js: str = "cdn", log_lines: list[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    for i, fig in enumerate(figures):
        parts.append(fig.to_html(full_html=False, include_plotlyjs=(plotly_js if i == 0 else False), config={"displaylogo": False}))
    tbl = "".join(f"<h2>{html.escape(name)}</h2>{df.to_html(index=False, border=0, classes='data')}" for name, df in (tables or []))
    log = ("<h2>Rule log (IST)</h2><pre>" + html.escape("\n".join(log_lines)) + "</pre>") if log_lines else ""
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --grid:#e1e0d9; }}
@media (prefers-color-scheme: dark) {{ :root {{ --surface:#1a1a19; --page:#0d0d0d; --ink:#f0efec; --ink2:#c3c2b7; --grid:#2c2c2a; }} }}
body {{ margin:0 auto; max-width:1280px; padding:16px; background:var(--page); color:var(--ink); font-family:system-ui,-apple-system,'Segoe UI',sans-serif; }}
h1 {{ font-size:20px; }} h2 {{ font-size:16px; margin-top:28px; }}
table {{ border-collapse:collapse; font-size:13px; }} th, td {{ text-align:left; padding:4px 10px; border-bottom:1px solid var(--grid); vertical-align:top; }}
table.data td {{ font-variant-numeric: tabular-nums; }} th {{ color:var(--ink2); font-weight:600; }}
pre {{ font-size:12px; white-space:pre-wrap; color:var(--ink2); }}
.note {{ color:var(--ink2); font-size:13px; }}
</style></head><body>
<h1>{html.escape(title)}</h1><p class="note">Decision support only, not financial advice. Charts support your review; they do not replace it.</p>
{_plan_html(plan) if plan is not None else ''}
{''.join(parts)}
{tbl}
{log}
</body></html>"""
    path.write_text(doc, encoding="utf-8")
    return path

