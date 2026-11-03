"""Exchange tick sizes and exact price rounding.

NSE assigns each stock a tick size from a price-slab table (kept in
``settings.yaml → ticks.table``). Every trigger, entry, stop and target must be a
multiple of that tick before it is used, and the risk calculator rounds *before*
it computes the share count.

All arithmetic here uses :class:`~decimal.Decimal`. Float inputs are first
cleaned to 6 decimals so that binary noise (``500.0000000001``) cannot push a
price up to the next tick.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings, TickSlab

RoundMode = Literal["up", "down", "nearest"]
_ROUNDING = {"up": ROUND_CEILING, "down": ROUND_FLOOR, "nearest": ROUND_HALF_UP}


def to_decimal(value: float | int | str | Decimal) -> Decimal:
    """Convert a price to Decimal, removing float noise beyond 6 decimals."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(round(value, 6)))
    return Decimal(str(value))


def round_to_tick(price: float | Decimal, tick: Decimal, mode: RoundMode) -> Decimal:
    """Round ``price`` to a multiple of ``tick`` (up, down or nearest)."""
    p = to_decimal(price)
    if tick <= 0:
        raise ValueError("tick must be positive")
    steps = (p / tick).to_integral_value(rounding=_ROUNDING[mode])
    return (steps * tick).quantize(tick)


def is_on_tick(price: float | Decimal, tick: Decimal) -> bool:
    p = to_decimal(price)
    return (p / tick) == (p / tick).to_integral_value()


class TickTable:
    """Price-slab tick table, e.g. < ₹250 → ₹0.01, ₹250–1,000 → ₹0.05, ..."""

    def __init__(self, slabs: Sequence["TickSlab"]):
        self._slabs = list(slabs)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "TickTable":
        return cls(settings.ticks.table)

    def tick_for(self, reference_price: float | Decimal) -> Decimal:
        """Tick size of the slab that ``reference_price`` falls into."""
        p = to_decimal(reference_price)
        for slab in self._slabs:
            if slab.max_price is None:
                return slab.tick
            if p < slab.max_price or (slab.max_inclusive and p == slab.max_price):
                return slab.tick
        return self._slabs[-1].tick  # pragma: no cover - last slab always matches

    def round(
        self,
        price: float | Decimal,
        mode: RoundMode,
        reference_price: float | Decimal | None = None,
        tick: Decimal | None = None,
    ) -> Decimal:
        """Round ``price`` using the stock's tick.

        ``tick`` wins when given (e.g. from the broker's instrument file).
        Otherwise the slab is chosen from ``reference_price`` (the stock's last
        close, as NSE reviews slabs on closing prices) or from ``price`` itself.
        """
        use_tick = tick if tick is not None else self.tick_for(reference_price if reference_price is not None else price)
        return round_to_tick(price, use_tick, mode)

    def add_ticks(self, price: float | Decimal, n: int, tick: Decimal) -> Decimal:
        return (to_decimal(price) + tick * n).quantize(tick)

    def normalise_instrument_tick(
        self, raw_tick: float, reference_price: float | Decimal, unit: Literal["auto", "rupees", "paise"]
    ) -> Decimal:
        """Interpret the tick_size field of a broker instrument file.

        Upstox has published this field both in rupees (0.05) and, in some
        files, apparently in paise (5.0). With ``unit='auto'`` the reading that
        matches the exchange tick table for the stock's price is chosen, and the
        table value is used if neither reading matches.
        """
        expected = self.tick_for(reference_price)
        as_rupees = to_decimal(raw_tick)
        as_paise = (to_decimal(raw_tick) / Decimal(100)).normalize()
        if unit == "rupees":
            return as_rupees
        if unit == "paise":
            return as_paise
        for candidate in (as_rupees, as_paise):
            if candidate == expected:
                return expected
        return expected
