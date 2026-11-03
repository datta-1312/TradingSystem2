"""Transaction charges for NSE cash trades (Upstox tariff + statutory levies).

Per executed order ("leg"), with every rate taken from ``settings.yaml → costs``:

    brokerage     min(₹ per order, cap % × turnover)
    STT           delivery: % on buy and on sell (intraday: sell only), rounded to the rupee
    exchange txn  % of turnover (NSE cash market)
    SEBI fee      ₹ per crore of turnover
    IPFT          ₹ per crore of turnover (NSE investor protection fund)
    stamp duty    buy side only
    GST           % of the components listed in ``gst_applies_to``
    DP charge     per scrip per sell day for delivery sells from the demat account

R-multiples are reported gross (price move only) and net of all charges. The
net R denominator is the initial planned risk: shares × (entry − stop).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Literal

from nseswing.core.ticks import round_to_tick, to_decimal

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings

PAISA = Decimal("0.01")
CRORE = Decimal("10000000")
HUNDRED = Decimal(100)
Side = Literal["BUY", "SELL"]


def _p(x: Decimal) -> Decimal:
    return x.quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass
class LegCharges:
    side: Side
    qty: int
    price: Decimal
    turnover: Decimal
    brokerage: Decimal = Decimal("0.00")
    stt: Decimal = Decimal("0.00")
    exchange_txn: Decimal = Decimal("0.00")
    sebi_fee: Decimal = Decimal("0.00")
    ipft: Decimal = Decimal("0.00")
    stamp_duty: Decimal = Decimal("0.00")
    dp: Decimal = Decimal("0.00")
    gst: Decimal = Decimal("0.00")
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.exchange_txn + self.sebi_fee + self.ipft + self.stamp_duty + self.dp + self.gst

    def as_dict(self) -> dict[str, str]:
        keys = ("brokerage", "stt", "exchange_txn", "sebi_fee", "ipft", "stamp_duty", "dp", "gst")
        out = {k: str(getattr(self, k)) for k in keys}
        out["total"] = str(self.total)
        return out


class ChargeModel:
    def __init__(self, settings: "Settings"):
        self.c = settings.costs
        self.product_default = settings.account.product

    def leg(self, side: Side, qty: int, price: float | Decimal, product: str | None = None, dp_applies: bool = True) -> LegCharges:
        c = self.c
        product = product or self.product_default
        px = to_decimal(price)
        turnover = px * qty
        leg = LegCharges(side, qty, px, turnover)
        if qty <= 0:
            return leg
        if product == "CNC":
            brokerage = min(c.brokerage.delivery_per_order_inr, turnover * c.brokerage.delivery_pct_cap / HUNDRED)
            stt_pct = c.stt.delivery_buy_pct if side == "BUY" else c.stt.delivery_sell_pct
            stamp_pct = c.stamp_duty.delivery_buy_pct
        else:
            brokerage = min(c.brokerage.intraday_per_order_inr, turnover * c.brokerage.intraday_pct_cap / HUNDRED)
            stt_pct = c.stt.intraday_sell_pct if side == "SELL" else Decimal(0)
            stamp_pct = c.stamp_duty.intraday_buy_pct
        leg.brokerage = _p(brokerage)
        stt = turnover * stt_pct / HUNDRED
        leg.stt = stt.quantize(Decimal(1), rounding=ROUND_HALF_UP).quantize(PAISA) if c.stt.round_to_rupee else _p(stt)
        leg.exchange_txn = _p(turnover * c.exchange_txn_pct / HUNDRED)
        leg.sebi_fee = _p(turnover * c.sebi_fee_per_crore_inr / CRORE)
        leg.ipft = _p(turnover * c.ipft_per_crore_inr / CRORE)
        leg.stamp_duty = _p(turnover * stamp_pct / HUNDRED) if side == "BUY" else Decimal("0.00")
        if side == "SELL" and product == "CNC" and dp_applies:
            leg.dp = _p(c.dp_charge_per_scrip_per_sell_day_inr)
        gst_base = sum((getattr(leg, k) for k in c.gst_applies_to), Decimal(0))
        leg.gst = _p(gst_base * c.gst_pct / HUNDRED)
        return leg

    def round_trip(self, qty: int, entry: float | Decimal, exit_price: float | Decimal, product: str | None = None, dp_applies: bool = True) -> tuple[LegCharges, LegCharges]:
        return self.leg("BUY", qty, entry, product), self.leg("SELL", qty, exit_price, product, dp_applies)

    def net_pnl(self, qty: int, entry: float | Decimal, exit_price: float | Decimal, product: str | None = None) -> Decimal:
        buy, sell = self.round_trip(qty, entry, exit_price, product)
        return (to_decimal(exit_price) - to_decimal(entry)) * qty - buy.total - sell.total

    def net_target_price(self, entry: Decimal, risk_per_share: Decimal, r_multiple: Decimal, qty: int, tick: Decimal, product: str | None = None) -> Decimal:
        """Lowest valid price at which the round trip nets ``r_multiple`` × the initial ₹ risk."""
        if qty <= 0:
            return round_to_tick(entry + risk_per_share * r_multiple, tick, "up")
        want = r_multiple * risk_per_share * qty
        lo, hi = entry, entry + risk_per_share * r_multiple * 2 + entry * Decimal("0.05")
        for _ in range(60):
            mid = (lo + hi) / 2
            if self.net_pnl(qty, entry, mid, product) >= want:
                hi = mid
            else:
                lo = mid
        price = round_to_tick(hi, tick, "up")
        while self.net_pnl(qty, entry, price, product) < want:
            price += tick
        return price


def r_multiple(pnl_per_share: Decimal, risk_per_share: Decimal) -> Decimal:
    return (pnl_per_share / risk_per_share).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if risk_per_share else Decimal(0)


def ceil_paise(x: Decimal) -> Decimal:
    return x.quantize(PAISA, rounding=ROUND_CEILING)
