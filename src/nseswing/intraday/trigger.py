"""Stage 2.6–2.8: the intraday entry trigger.

Per stock and session, zones are watched from the highest down:

    WAITING ──price within approach % of the zone──► APPROACH (monitor the 1/5/15-min chart)
       │                                                  │
       └──────────────── bar low enters the zone ◄───────┘
                                   │
                                ARMED  (price has dipped into support)
                     ┌─────────────┴──────────────┐
     trades above the previous bar's high      bar closes below the zone
     (+ trigger_offset_ticks)                  (− tolerance) with no trigger
                     │                              │
                 TRIGGERED                      BROKEN → zone remembered as broken,
                 (validity checks)              keep waiting at the next zone below

A trigger is valid only if the stock still passes the Stage 1 daily
conditions, price is at the zone, continuous trading is open for the stock
(CAS stocks stop at 15:15), and the price is below the upper circuit.

OHLC replay: when only bar OHLC is known, a *green* bar is assumed to trade
open → low → high → close and a *red* bar open → high → low → close. A bar that
dips into the zone can therefore trigger in the same bar only if it is green.
Live (intrabar) evaluation uses the actual tick sequence instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Literal

import pandas as pd

from nseswing.core.ticks import round_to_tick, to_decimal
from nseswing.core.timeutil import fmt_ist, to_ist
from nseswing.intraday.bars import Bar
from nseswing.levels import Zone

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings
    from nseswing.core.calendar import Session
    from nseswing.data.reference import CircuitLimits
    from nseswing.levels import ZoneMemory


class State(str, Enum):
    WAITING = "waiting"
    APPROACH = "approach"
    ARMED = "armed"
    TRIGGERED = "triggered"
    DONE = "done"


EventKind = Literal["approach", "dip", "trigger", "invalid_trigger", "zone_broken", "next_zone", "no_zones", "session_end"]


@dataclass
class TriggerEvent:
    ts: pd.Timestamp
    kind: EventKind
    symbol: str
    message: str
    zone: Zone | None = None
    price: float | None = None

    def __str__(self) -> str:
        return f"{fmt_ist(self.ts)}  {self.symbol:<12} {self.kind.upper():<16} {self.message}"


@dataclass
class TriggerSignal:
    symbol: str
    zone: Zone
    ts: pd.Timestamp
    trigger_price: Decimal  # previous bar high + offset ticks
    fill_reference: Decimal  # where a buy-stop at the trigger would fill (bar open if it gapped above)
    trigger_bar: Bar
    prior_bar: Bar
    day_low: float
    dip_low: float
    valid: bool
    invalid_reasons: list[str] = field(default_factory=list)


class TriggerEngine:
    def __init__(
        self,
        symbol: str,
        zones: list[Zone],
        tick: Decimal,
        settings: "Settings",
        session: "Session",
        stage1_pass: bool,
        cas_stock: bool = False,
        circuit: "CircuitLimits | None" = None,
        memory: "ZoneMemory | None" = None,
    ):
        self.symbol = symbol
        self.zones = sorted([z for z in zones if not z.broken], key=lambda z: -float(z.top))
        self.tick = tick
        self.s = settings
        self.cfg = settings.intraday
        self.session = session
        self.stage1_pass = stage1_pass
        self.cas_stock = cas_stock
        self.circuit = circuit
        self.memory = memory
        self.idx = 0
        self.state = State.WAITING if self.zones else State.DONE
        self.bars: list[Bar] = []
        self.dip_low: float | None = None
        self.day_low: float | None = None
        self.entries = 0
        self.signal: TriggerSignal | None = None
        self.events: list[TriggerEvent] = []

    # ------------------------------------------------------------------ helpers
    @property
    def zone(self) -> Zone | None:
        return self.zones[self.idx] if self.idx < len(self.zones) else None

    def _emit(self, ts, kind: EventKind, message: str, price: float | None = None) -> TriggerEvent:
        ev = TriggerEvent(to_ist(ts), kind, self.symbol, message, self.zone, price)
        self.events.append(ev)
        return ev

    def _break_level(self, z: Zone) -> float:
        return float(z.bottom) * (1 - self.cfg.zone_break.tolerance_pct / 100)

    def trigger_level(self) -> Decimal | None:
        if not self.bars:
            return None
        prev_high = to_decimal(self.bars[-1].high)
        return round_to_tick(prev_high + self.tick * self.cfg.trigger_offset_ticks, self.tick, "up")

    # ------------------------------------------------------------------ bar-close evaluation (replay and live)
    def on_bar(self, bar: Bar) -> list[TriggerEvent]:
        """Feed one completed bar. Returns the events it caused."""
        start = len(self.events)
        self.day_low = bar.low if self.day_low is None else min(self.day_low, bar.low)
        if self.state in (State.DONE, State.TRIGGERED):
            self.bars.append(bar)
            return []
        if not self.session.is_continuous(bar.start, self.cas_stock):
            self.bars.append(bar)
            return []
        green = bar.close >= bar.open
        level = self.trigger_level()
        z = self.zone
        assert z is not None

        # 1. already armed: does this bar trade above the previous bar's high?
        if self.state == State.ARMED and level is not None and bar.high >= float(level):
            self._fire(bar, level)
            self.bars.append(bar)
            return self.events[start:]

        # 2. not armed yet: approach / dip into the zone
        if self.state in (State.WAITING, State.APPROACH):
            if bar.low <= float(z.top):
                self.state = State.ARMED
                self.dip_low = bar.low
                self._emit(bar.start, "dip", f"dipped into zone {z.describe()} (low ₹{bar.low:.2f})", bar.low)
                if green and level is not None and bar.high >= float(level) and bar.close >= self._break_level(z):
                    self._fire(bar, level)  # green bar: low came before the high
                    self.bars.append(bar)
                    return self.events[start:]
            elif self.state == State.WAITING and bar.low <= float(z.top) * (1 + self.cfg.approach_pct / 100):
                self.state = State.APPROACH
                self._emit(bar.start, "approach", f"within {self.cfg.approach_pct}% of zone top ₹{z.top}: monitoring {self.cfg.timeframe_minutes}-min chart", bar.low)
        else:  # ARMED
            self.dip_low = min(self.dip_low or bar.low, bar.low)

        # 3. zone broken? (close below the zone, or trade below with trade_below)
        if self.state == State.ARMED:
            broke_value = bar.close if self.cfg.zone_break.rule == "close_below" else bar.low
            self._check_breaks(bar, broke_value)
        self.bars.append(bar)
        return self.events[start:]

    def _check_breaks(self, bar: Bar, value: float) -> None:
        while self.state == State.ARMED and self.zone is not None and value < self._break_level(self.zone):
            z = self.zone
            self._emit(bar.start, "zone_broken", f"fell through zone ₹{z.bottom}–₹{z.top} without a rebound: no trade, zone marked broken", value)
            z.broken, z.broken_on = True, bar.start.date()
            if self.memory is not None:
                self.memory.mark_broken(self.symbol, z.bottom, z.top, bar.start.date(), "intraday break")
            self.idx += 1
            self.dip_low = None
            nz = self.zone
            if nz is None:
                self.state = State.DONE
                self._emit(bar.start, "no_zones", "no support zones left today")
                return
            self.state = State.WAITING
            self._emit(bar.start, "next_zone", f"waiting at the next zone {nz.describe()}")
            if bar.low <= float(nz.top):
                self.state = State.ARMED
                self.dip_low = bar.low
                self._emit(bar.start, "dip", f"dipped into zone {nz.describe()} (low ₹{bar.low:.2f})", bar.low)

    # ------------------------------------------------------------------ intrabar evaluation on 1-min bars (5/15-min charts)
    def check_intrabar(self, forming: Bar, minute: Bar) -> list[TriggerEvent]:
        """Watch the 5- or 15-minute chart one minute at a time.

        ``forming`` is the monitored-timeframe bar built so far; ``minute`` is the
        newest 1-minute bar inside it. The trigger level is still the previous
        *completed* monitored bar's high, so a 5-min setup can fire mid-bar.
        """
        start = len(self.events)
        if self.state in (State.DONE, State.TRIGGERED) or not self.session.is_continuous(minute.start, self.cas_stock):
            return []
        z = self.zone
        assert z is not None
        self.day_low = minute.low if self.day_low is None else min(self.day_low, minute.low)
        level = self.trigger_level()
        if self.state in (State.WAITING, State.APPROACH):
            if minute.low <= float(z.top):
                self.state, self.dip_low = State.ARMED, minute.low
                self._emit(minute.start, "dip", f"dipped into zone {z.describe()} (low ₹{minute.low:.2f})", minute.low)
                if minute.close >= minute.open and level is not None and minute.high >= float(level):
                    self._fire(forming, level, fill_price=max(level, round_to_tick(minute.open, self.tick, "up")), ts=minute.start)
            elif self.state == State.WAITING and minute.low <= float(z.top) * (1 + self.cfg.approach_pct / 100):
                self.state = State.APPROACH
                self._emit(minute.start, "approach", f"within {self.cfg.approach_pct}% of zone top ₹{z.top}: monitoring {self.cfg.timeframe_minutes}-min chart", minute.low)
            return self.events[start:]
        self.dip_low = min(self.dip_low or minute.low, minute.low)
        if level is not None and minute.high >= float(level):
            self._fire(forming, level, fill_price=max(level, round_to_tick(minute.open, self.tick, "up")), ts=minute.start)
        return self.events[start:]

    # ------------------------------------------------------------------ intrabar evaluation (live ticks)
    def on_price(self, ts: datetime, price: float) -> list[TriggerEvent]:
        """Live tick: arm on entry into the zone, fire on the first trade above the previous bar's high."""
        start = len(self.events)
        if self.state in (State.DONE, State.TRIGGERED) or not self.session.is_continuous(ts, self.cas_stock):
            return []
        z = self.zone
        assert z is not None
        self.day_low = price if self.day_low is None else min(self.day_low, price)
        if self.state in (State.WAITING, State.APPROACH) and price <= float(z.top):
            self.state, self.dip_low = State.ARMED, price
            self._emit(ts, "dip", f"traded into zone {z.describe()} at ₹{price:.2f}", price)
            return self.events[start:]
        if self.state == State.ARMED:
            self.dip_low = min(self.dip_low or price, price)
            level = self.trigger_level()
            if level is not None and price >= float(level):
                t = to_ist(ts)
                forming = Bar(t, t, price, price, min(price, self.dip_low), price, 0.0)
                self._fire(forming, level, fill_price=to_decimal(price), ts=t)
        if self.state == State.ARMED and self.cfg.zone_break.rule == "trade_below":
            t = to_ist(ts)
            self._check_breaks(Bar(t, t, price, price, price, price, 0.0), price)
        return self.events[start:]

    # ------------------------------------------------------------------ firing and validation
    def _fire(self, bar: Bar, level: Decimal, fill_price: Decimal | None = None, ts: pd.Timestamp | None = None) -> None:
        z = self.zone
        assert z is not None
        prior = self.bars[-1]
        when = to_ist(ts) if ts is not None else to_ist(bar.start)
        fill = fill_price if fill_price is not None else max(level, round_to_tick(bar.open, self.tick, "up"))
        reasons = self._validate(bar, level, z, when)
        dip_low = min(self.dip_low if self.dip_low is not None else bar.low, bar.low)
        day_low = min(self.day_low if self.day_low is not None else bar.low, bar.low)
        sig = TriggerSignal(self.symbol, z, when, level, fill, bar, prior, day_low, dip_low, not reasons, reasons)
        if reasons:
            self._emit(when, "invalid_trigger", f"traded above previous bar high ₹{prior.high:.2f} but INVALID: {'; '.join(reasons)}", float(level))
            self.state = State.DONE if any("Stage 1" in r or "session" in r or "entries" in r or "after" in r for r in reasons) else State.WAITING
            return
        self.signal = sig
        self.entries += 1
        self.state = State.TRIGGERED
        self._emit(when, "trigger", f"traded ₹{level} > previous bar high ₹{prior.high:.2f} after dipping to ₹{dip_low:.2f} in zone ₹{z.bottom}–₹{z.top}", float(level))

    def _validate(self, bar: Bar, level: Decimal, z: Zone, when: pd.Timestamp) -> list[str]:
        reasons = []
        if not self.stage1_pass:
            reasons.append("stock no longer passes the Stage 1 daily conditions")
        dip = min(self.dip_low if self.dip_low is not None else bar.low, bar.low)
        if dip < self._break_level(z):
            reasons.append(f"dip ₹{dip:.2f} went below the zone bottom ₹{z.bottom}")
        max_px = float(z.top) * (1 + self.cfg.at_zone_max_distance_pct / 100)
        if float(level) > max_px:
            reasons.append(f"trigger ₹{level} is more than {self.cfg.at_zone_max_distance_pct}% above the zone (not at support)")
        if not self.session.is_continuous(when, self.cas_stock):
            reasons.append("continuous session closed for this stock")
        cutoff = self.cfg.no_new_entries_after
        if cutoff is not None and to_ist(when).time() >= cutoff:
            reasons.append(f"after the no-new-entries time {cutoff:%H:%M}")
        if self.entries >= self.cfg.max_entries_per_symbol_per_day:
            reasons.append("max entries for this stock today already taken")
        if self.circuit is not None and level >= self.circuit.upper:
            reasons.append(f"at the upper circuit ₹{self.circuit.upper}: a buy cannot fill")
        return reasons
