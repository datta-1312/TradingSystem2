"""Rupee formatting with Indian digit grouping (lakh / crore).

    format_inr(1000000)        -> '₹10,00,000.00'
    format_inr(5000, 0)        -> '₹5,000'
    format_inr_short(12500000) -> '₹1.25 Cr'
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

LAKH = Decimal("100000")
CRORE = Decimal("10000000")


def _to_dec(value: float | int | str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def group_indian(digits: str) -> str:
    """Group an unsigned integer string the Indian way: last 3 digits, then pairs."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join(pairs) + "," + tail


def format_inr(amount: float | int | Decimal, decimals: int = 2, symbol: bool = True, signed: bool = False) -> str:
    """``₹12,34,567.89``; negatives render as ``-₹5,000.00``."""
    value = _to_dec(amount)
    quantum = Decimal(1).scaleb(-decimals) if decimals > 0 else Decimal(1)
    value = value.quantize(quantum, rounding=ROUND_HALF_UP)
    negative = value < 0
    text = f"{abs(value):.{decimals}f}"
    int_part, _, frac_part = text.partition(".")
    body = group_indian(int_part) + (f".{frac_part}" if decimals > 0 else "")
    sign = "-" if negative else ("+" if signed and value > 0 else "")
    return f"{sign}{'₹' if symbol else ''}{body}"


def format_inr_short(amount: float | int | Decimal, decimals: int = 2) -> str:
    """Lakh / crore short form for dashboards: ``₹2.00 L``, ``₹1.25 Cr``."""
    value = _to_dec(amount)
    negative = value < 0
    mag = abs(value)
    sign = "-" if negative else ""
    if mag >= CRORE:
        return f"{sign}₹{(mag / CRORE):.{decimals}f} Cr"
    if mag >= LAKH:
        return f"{sign}₹{(mag / LAKH):.{decimals}f} L"
    return sign + format_inr(mag, 0)


def format_pct(value: float | Decimal, decimals: int = 2, signed: bool = False) -> str:
    v = float(value)
    return f"{v:+.{decimals}f}%" if signed else f"{v:.{decimals}f}%"


def format_price(value: float | Decimal, decimals: int = 2) -> str:
    """A share price: ₹ with grouping, always 2 decimals (ticks are ≥ ₹0.01)."""
    return format_inr(value, decimals)
