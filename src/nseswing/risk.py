"""Stage 3: stop-loss and position size, worked out before any order.

Calculation order (method 3.4):

1. Entry and stop are set and **rounded to valid ticks** (entry up, stop down by default).
2. Risk per share = entry − stop.
3. Shares = allowed ₹ loss ÷ risk per share, rounded down.
4. Then position value and % of account.

The stop comes from the trigger (method 3.1): the day's low, the trigger bar's
low or the low of the bar before it. Stops wider than ``risk.max_stop_pct_warn``
raise a warning but never block the plan. CNC buys are capped by available cash
(no leverage in delivery).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from nseswing.core.money import format_inr
from nseswing.core.ticks import TickTable, round_to_tick, to_decimal
from nseswing.costs import ChargeModel, LegCharges

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings
    from nseswing.data.reference import CircuitLimits
    from nseswing.intraday.trigger import TriggerSignal
    from nseswing.levels import Zone

HUNDRED = Decimal(100)
TWO_DP = Decimal("0.01")


class InvalidPlan(ValueError):
    pass


@dataclass
class TradePlan:
    symbol: str
    entry: Decimal
    stop: Decimal
    tick: Decimal
    risk_per_share: Decimal
    max_loss: Decimal
    shares: int
    position_value: Decimal
    position_pct: Decimal
    stop_pct: Decimal
    planned_loss: Decimal  # shares × risk per share (≤ max_loss)
    targets: dict[str, Decimal]  # gross: "1R", "3R", "5R"
    net_targets: dict[str, Decimal]  # price that nets k R after all charges
    net_r_at_target: dict[str, Decimal]  # net R if you exit exactly at the gross target
    entry_charges: LegCharges
    net_loss_at_stop: Decimal
    stop_method: str
    stop_raw: float | None
    equity: Decimal
    risk_pct: Decimal
    product: str
    theme: str = ""
    zone: "Zone | None" = None
    trigger_price: Decimal | None = None
    circuit: "CircuitLimits | None" = None
    lower_circuit_loss: Decimal | None = None
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    is_draft: bool = False
    created_at: datetime | None = None
    stop_buffer_ticks: int = 0  # ticks the stop sits below the chosen low (risk.stop_buffer_ticks)

    @property
    def r(self) -> Decimal:
        return self.risk_per_share

    @property
    def stop_basis(self) -> str:
        """How the stop was chosen, e.g. ``day low ₹472.90 − 1 tick``."""
        label = self.stop_method.replace("_", " ")
        n = self.stop_buffer_ticks
        if not n:
            return label
        return f"{label} ₹{self.stop + self.tick * n} − {n} tick{'s' if n != 1 else ''}"


class RiskCalculator:
    def __init__(self, settings: "Settings", ticks: TickTable | None = None, costs: ChargeModel | None = None):
        self.s = settings
        self.ticks = ticks or TickTable.from_settings(settings)
        self.costs = costs or ChargeModel(settings)

    # ------------------------------------------------------------------ stop selection (3.1)
    def choose_stop(self, signal: "TriggerSignal", tick: Decimal, method: str | None = None) -> tuple[Decimal, float, str]:
        method = method or self.s.risk.stop_method
        candidates = {
            "day_low": signal.day_low,
            "trigger_bar_low": signal.trigger_bar.low,
            "prior_bar_low": signal.prior_bar.low,
        }
        raw = candidates[method]
        stop = round_to_tick(raw, tick, self.s.ticks.rounding.stop) - tick * self.s.risk.stop_buffer_ticks
        return stop, raw, method

    # ------------------------------------------------------------------ the plan (3.3–3.6, 4.1)
    def plan(
        self,
        symbol: str,
        entry: float | Decimal,
        stop: float | Decimal,
        tick: Decimal | None = None,
        reference_price: float | Decimal | None = None,
        stop_method: str = "manual",
        stop_raw: float | None = None,
        equity: Decimal | None = None,
        available_cash: Decimal | None = None,
        zone: "Zone | None" = None,
        trigger_price: Decimal | None = None,
        theme: str = "",
        flags: list[str] | None = None,
        circuit: "CircuitLimits | None" = None,
        is_draft: bool = False,
        created_at: datetime | None = None,
    ) -> TradePlan:
        s = self.s
        rounding = s.ticks.rounding
        tick = tick or self.ticks.tick_for(reference_price if reference_price is not None else entry)
        entry_r = round_to_tick(entry, tick, rounding.entry)
        stop_r = round_to_tick(stop, tick, rounding.stop)
        if stop_r >= entry_r:
            raise InvalidPlan(f"stop ₹{stop_r} must be below entry ₹{entry_r}")
        equity = equity if equity is not None else s.account.equity_inr
        risk_pct = s.account.risk_per_trade_pct
        rps = entry_r - stop_r
        max_loss = (equity * risk_pct / HUNDRED).quantize(TWO_DP, rounding=ROUND_HALF_UP)
        shares = int((max_loss / rps).to_integral_value(rounding=ROUND_FLOOR))
        warnings: list[str] = []
        info: list[str] = []

        cash = available_cash if available_cash is not None else (s.account.available_cash_inr or equity)
        if s.risk.cap_to_available_cash and shares * entry_r > cash:
            capped = int((cash / entry_r).to_integral_value(rounding=ROUND_FLOOR))
            warnings.append(f"Position capped by available cash {format_inr(cash, 0)}: {shares} → {capped} shares (risk below the 0.5% budget)")
            shares = capped
        if s.risk.max_position_pct is not None:
            limit = int((equity * s.risk.max_position_pct / HUNDRED / entry_r).to_integral_value(rounding=ROUND_FLOOR))
            if shares > limit:
                warnings.append(f"Position capped at {s.risk.max_position_pct}% of equity: {shares} → {limit} shares")
                shares = limit
        if shares <= 0:
            warnings.append("Risk per share exceeds the allowed loss: no position possible at this stop")

        position = entry_r * shares
        position_pct = (position / equity * HUNDRED).quantize(TWO_DP, rounding=ROUND_HALF_UP)
        stop_pct = (rps / entry_r * HUNDRED).quantize(TWO_DP, rounding=ROUND_HALF_UP)
        if stop_pct > s.risk.max_stop_pct_warn:
            warnings.append(f"Stop distance {stop_pct}% is wider than the {s.risk.typical_stop_pct_min}–{s.risk.max_stop_pct_warn}% target (warning only)")
        elif stop_pct < s.risk.typical_stop_pct_min:
            info.append(f"Stop distance {stop_pct}% is tighter than the usual {s.risk.typical_stop_pct_min}–{s.risk.max_stop_pct_warn}%, so the position is larger ({position_pct}% of account)")

        targets, net_targets, net_r = {}, {}, {}
        risk_rupees = rps * shares
        for k in s.targets.r_multiples:
            label = f"{k.normalize()}R"
            gross = round_to_tick(entry_r + rps * k, tick, rounding.target)
            targets[label] = gross
            net_targets[label] = self.costs.net_target_price(entry_r, rps, k, shares, tick)
            if shares > 0:
                net_r[label] = (self.costs.net_pnl(shares, entry_r, gross) / risk_rupees).quantize(TWO_DP, rounding=ROUND_HALF_UP)
        entry_charges = self.costs.leg("BUY", shares, entry_r)
        net_loss = -self.costs.net_pnl(shares, entry_r, stop_r) if shares else Decimal(0)

        lower_loss = None
        if circuit is not None and shares:
            lower_loss = ((entry_r - circuit.lower) * shares).quantize(TWO_DP)
        warnings.append(
            f"Gap-downs, slippage or a lower-circuit lock can fill below the stop, so the real loss can exceed the planned {format_inr(rps * shares, 0)}"
            + (f" (a fill at today's lower circuit ₹{circuit.lower} would lose {format_inr(lower_loss, 0)})" if lower_loss is not None else "")
        )
        if s.account.product == "CNC":
            info.append(
                "Delivery (CNC): place the stop immediately after the fill; a DAY SL expires at the close (CAS stocks: cancelled at 15:15), "
                "so convert it to a GTT or re-place it at 09:15. Automated sells from holdings need DDPI or eDIS/TPIN authorisation."
            )
        return TradePlan(
            symbol=symbol,
            entry=entry_r,
            stop=stop_r,
            tick=tick,
            risk_per_share=rps,
            max_loss=max_loss,
            shares=shares,
            position_value=position,
            position_pct=position_pct,
            stop_pct=stop_pct,
            planned_loss=rps * shares,
            targets=targets,
            net_targets=net_targets,
            net_r_at_target=net_r,
            entry_charges=entry_charges,
            net_loss_at_stop=net_loss,
            stop_method=stop_method,
            stop_raw=stop_raw,
            equity=equity,
            risk_pct=risk_pct,
            product=s.account.product,
            theme=theme,
            zone=zone,
            trigger_price=trigger_price,
            circuit=circuit,
            lower_circuit_loss=lower_loss,
            warnings=warnings,
            info=info,
            flags=list(flags or []),
            is_draft=is_draft,
            created_at=created_at,
        )

    def plan_from_signal(self, signal: "TriggerSignal", tick: Decimal, **kw) -> TradePlan:
        stop, raw, method = self.choose_stop(signal, tick)
        plan = self.plan(
            signal.symbol,
            signal.fill_reference,
            stop,
            tick=tick,
            stop_method=method,
            stop_raw=raw,
            zone=signal.zone,
            trigger_price=signal.trigger_price,
            created_at=signal.ts.to_pydatetime(),
            **kw,
        )
        plan.stop_buffer_ticks = self.s.risk.stop_buffer_ticks
        return plan

    def draft_plan(self, symbol: str, zone: "Zone", tick: Decimal, **kw) -> TradePlan:
        """Pre-market estimate: entry near the zone top, stop just under the zone.

        The final numbers are recomputed from the actual trigger bar.
        """
        entry = round_to_tick(to_decimal(zone.top) + tick * self.s.intraday.trigger_offset_ticks, tick, "up")
        stop = round_to_tick(to_decimal(zone.bottom) * (1 - Decimal(str(self.s.intraday.zone_break.tolerance_pct)) / HUNDRED), tick, "down")
        plan = self.plan(symbol, entry, stop, tick=tick, stop_method="draft: zone bottom", zone=zone, is_draft=True, **kw)
        plan.info.insert(0, "DRAFT: estimated from the zone; entry, stop and shares are recomputed from the actual trigger bar")
        return plan
