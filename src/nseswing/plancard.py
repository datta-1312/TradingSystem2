"""Trade-plan card: one per setup, for the CLI (text) and the dashboard (Markdown).

Shows symbol, theme, zone, trigger level, stop, shares, ₹ risk, position %,
1R/3R/5R targets (gross and net of charges), circuit limits and surveillance
flags, plus every warning from the risk calculator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nseswing.core.money import format_inr

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.risk import TradePlan


def _rows(plan: "TradePlan") -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    rows.append(("Theme", plan.theme or "—"))
    rows.append(("Zone", plan.zone.describe() if plan.zone else "—"))
    if plan.trigger_price is not None:
        rows.append(("Trigger", f"traded above the previous bar's high at ₹{plan.trigger_price}"))
    elif plan.is_draft:
        rows.append(("Trigger", f"after a dip into the zone, a trade above the previous bar's high (≈ ₹{plan.entry})"))
    rows.append(("Entry", f"₹{plan.entry}" + (" (estimate)" if plan.is_draft else "")))
    rows.append(("Stop", f"₹{plan.stop}  [{plan.stop_basis}]  distance {plan.stop_pct}%"))
    rows.append(("Shares", f"{plan.shares:,}"))
    rows.append(("Risk", f"₹{plan.risk_per_share}/share × {plan.shares:,} = {format_inr(plan.planned_loss)} (budget {format_inr(plan.max_loss)} = {plan.risk_pct}% of {format_inr(plan.equity, 0)})"))
    rows.append(("Position", f"{format_inr(plan.position_value)} = {plan.position_pct}% of account"))
    gross = " · ".join(f"{k} ₹{v}" for k, v in plan.targets.items())
    rows.append(("Targets (gross)", f"{gross}   [R = ₹{plan.risk_per_share}/share]"))
    net = " · ".join(f"{k} ₹{v}" for k, v in plan.net_targets.items() if k != "1R")
    at = " · ".join(f"{k} → {v}R" for k, v in plan.net_r_at_target.items() if k != "1R")
    rows.append(("Targets (net)", f"{net}  (selling at the gross targets nets {at})"))
    rows.append(("Charges", f"entry ≈ {format_inr(plan.entry_charges.total)}; a stop-out loses ≈ {format_inr(plan.net_loss_at_stop)} net"))
    if plan.circuit is not None:
        band = f"{plan.circuit.band_pct:g}% band" if plan.circuit.band_pct else "no fixed band (F&O)"
        rows.append(("Circuits", f"₹{plan.circuit.lower} – ₹{plan.circuit.upper} ({band}, {plan.circuit.source})"))
    rows.append(("Flags", "; ".join(plan.flags) if plan.flags else "none"))
    rows.append(("Product", f"{plan.product} (delivery), T+1 settlement" if plan.product == "CNC" else plan.product))
    return rows


def render_text(plan: "TradePlan") -> str:
    title = f" TRADE PLAN · {plan.symbol}{' (DRAFT)' if plan.is_draft else ''} "
    rows = _rows(plan)
    width = max(len(k) for k, _ in rows)
    lines = ["┌" + title.center(78, "─") + "┐"]
    for k, v in rows:
        lines.append(f"│ {k:<{width}}  {v}")
    for w in plan.warnings:
        lines.append(f"│ ⚠ {w}")
    for i in plan.info:
        lines.append(f"│ ℹ {i}")
    lines.append("│ Decision support only, not financial advice. Review the charts before acting.")
    lines.append("└" + "─" * 78 + "┘")
    return "\n".join(lines)


def render_markdown(plan: "TradePlan") -> str:
    head = f"### Trade plan · {plan.symbol}{' (draft)' if plan.is_draft else ''}\n\n| | |\n|---|---|\n"
    body = "\n".join(f"| **{k}** | {v} |" for k, v in _rows(plan))
    notes = "".join(f"\n- ⚠ {w}" for w in plan.warnings) + "".join(f"\n- ℹ {i}" for i in plan.info)
    return head + body + ("\n\n**Notes**" + notes if notes else "") + "\n\n_Decision support only, not financial advice._"
