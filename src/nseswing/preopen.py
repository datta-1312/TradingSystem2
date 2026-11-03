"""Stage 2.5 (before the open): stocks whose support zones sit near the expected open.

The expected open comes from the pre-open call auction (09:00–09:15 IST): the
indicative equilibrium price from the Upstox quote (or NSE's pre-open feed), or
the previous close when nothing is available yet. Order entry closes at a random
moment around 09:08–09:10 (since 7 Sep 2026), after which the price is final.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from nseswing.levels import LevelMap, Zone, _PRIORITY_ORDER

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings
    from nseswing.data.base import Quote
    from nseswing.data.nse import PreopenInfo
    from nseswing.screener import ScreenResult

Status = Literal["inside", "near", "far", "opened_below"]


@dataclass
class PreopenCandidate:
    symbol: str
    expected_open: float
    source: str
    prev_close: float
    gap_pct: float
    zone: Zone | None
    distance_pct: float | None  # (open − zone top) / open × 100; ≤ 0 means at/inside the zone
    status: Status
    broken_at_open: list[Zone]
    note: str


def expected_open(
    symbol: str, prev_close: float, settings: "Settings", quote: "Quote | None" = None, preopen: "PreopenInfo | None" = None
) -> tuple[float, str]:
    src = settings.preopen.expected_open_source
    if src == "upstox_quote" and quote is not None and quote.ltp:
        return float(quote.ltp), "upstox pre-open quote"
    if src == "nse_preopen" and preopen is not None:
        return float(preopen.iep), "NSE pre-open IEP"
    if preopen is not None:
        return float(preopen.iep), "NSE pre-open IEP"
    if quote is not None and quote.ltp:
        return float(quote.ltp), "upstox quote"
    return float(prev_close), "previous close (no pre-open data)"


def classify(level_map: LevelMap, open_price: float, settings: "Settings") -> tuple[Zone | None, float | None, Status, list[Zone]]:
    """Find the first unbroken support zone at or below the expected open."""
    broken_now: list[Zone] = []
    for z in level_map.support_zones():
        if open_price < float(z.bottom):
            if settings.preopen.gap_below_zone_marks_broken:
                broken_now.append(z)
            continue
        dist = (open_price - float(z.top)) / open_price * 100
        if z.contains(open_price):
            return z, dist, "inside", broken_now
        if dist <= settings.preopen.near_zone_pct:
            return z, dist, "near", broken_now
        return z, dist, "far", broken_now
    status: Status = "opened_below" if broken_now else "far"
    return None, None, status, broken_now


def preopen_list(
    screen: "ScreenResult",
    level_maps: dict[str, LevelMap],
    settings: "Settings",
    quotes: "dict[str, Quote] | None" = None,
    preopen: "dict[str, PreopenInfo] | None" = None,
    include_far: bool = False,
) -> list[PreopenCandidate]:
    out: list[PreopenCandidate] = []
    for row in screen.watchlist:
        if not row.all_checks_pass or row.symbol not in level_maps:
            continue
        lm = level_maps[row.symbol]
        prev = lm.last_close
        eo, src = expected_open(row.symbol, prev, settings, (quotes or {}).get(row.symbol), (preopen or {}).get(row.symbol))
        zone, dist, status, broken = classify(lm, eo, settings)
        gap = (eo / prev - 1) * 100
        if status == "far" and not include_far:
            continue
        if zone is None:
            note = "opened below every support zone: no trade unless price reclaims a zone"
        elif status == "inside":
            note = f"expected open ₹{eo:.2f} is inside the zone: watch for a dip and a break of the previous bar's high"
        elif status == "near":
            note = f"zone top {dist:.2f}% below the expected open: watch for the pullback into it"
        else:
            note = f"zone {dist:.2f}% below the open (beyond {settings.preopen.near_zone_pct}%)"
        if broken:
            note += f"; {len(broken)} zone(s) above would open broken"
        out.append(PreopenCandidate(row.symbol, eo, src, prev, gap, zone, dist, status, broken, note))
    rank = {"inside": 0, "near": 1, "far": 2, "opened_below": 3}
    out.sort(key=lambda c: (rank[c.status], _PRIORITY_ORDER[c.zone.priority] if c.zone else 9, not (c.zone and c.zone.first_retest), c.distance_pct or 0))
    return out
