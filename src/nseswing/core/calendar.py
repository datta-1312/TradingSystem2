"""NSE trading calendar and per-date session times (IST).

Holidays and special sessions come from ``config/nse_calendar.yaml``; session
times and the Closing Auction Session (CAS) profile come from
``settings.yaml → sessions``.

CAS (NSE, from 3 Aug 2026): for stocks with F&O contracts, continuous trading
ends at 15:15 and is followed by a closing auction; pending stop-loss orders are
cancelled at that transition. Other stocks trade continuously until 15:30.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml

from nseswing.core.timeutil import IST, at, to_ist

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings

log = logging.getLogger(__name__)

Phase = Literal["closed", "preopen", "continuous", "closing_auction", "post_close"]


@dataclass(frozen=True)
class SpecialSession:
    date: date
    name: str
    preopen_start: time
    market_open: time
    market_close: time
    cas_continuous_end: time | None


@dataclass(frozen=True)
class Session:
    """One trading session. ``cas_continuous_end`` is None when CAS does not apply that day."""

    date: date
    preopen_start: datetime
    preopen_order_entry_end: datetime
    preopen_iep_final_after: datetime
    market_open: datetime
    market_close: datetime
    cas_continuous_end: datetime | None
    cas_auction_end: datetime | None
    special: str | None

    def continuous_end(self, cas_stock: bool) -> datetime:
        """When continuous trading (and with it trigger monitoring) ends for a stock."""
        if cas_stock and self.cas_continuous_end is not None:
            return self.cas_continuous_end
        return self.market_close

    def day_order_expiry(self, cas_stock: bool) -> datetime:
        """When a resting DAY stop-loss order stops protecting the position.

        For CAS stocks the exchange cancels pending SL orders when the stock
        moves into the closing auction; for others DAY orders expire at the close.
        """
        return self.continuous_end(cas_stock)

    def phase(self, ts: datetime, cas_stock: bool) -> Phase:
        t = to_ist(ts).to_pydatetime()
        if t < self.preopen_start:
            return "closed"
        if t < self.market_open:
            return "preopen"
        end = self.continuous_end(cas_stock)
        if t < end:
            return "continuous"
        if cas_stock and self.cas_continuous_end is not None and self.cas_auction_end is not None and t < self.cas_auction_end:
            return "closing_auction"
        return "post_close"

    def is_continuous(self, ts: datetime, cas_stock: bool) -> bool:
        return self.phase(ts, cas_stock) == "continuous"


class CalendarYearMissing(RuntimeError):
    pass


class NSECalendar:
    def __init__(
        self,
        holidays: dict[date, str],
        special_sessions: dict[date, SpecialSession],
        years_covered: set[int],
        settings: "Settings",
    ):
        self.holidays = holidays
        self.special_sessions = special_sessions
        self.years_covered = years_covered
        self._s = settings.sessions
        self._fail_if_missing = settings.calendar.fail_if_year_missing
        self._warned_years: set[int] = set()

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_settings(cls, settings: "Settings", path: str | Path | None = None) -> "NSECalendar":
        file = Path(path) if path else settings.path(settings.calendar.file)
        with file.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        holidays = {_as_date(h["date"]): str(h["name"]) for h in raw.get("holidays", [])}
        specials: dict[date, SpecialSession] = {}
        for item in raw.get("special_sessions", []):
            d = _as_date(item["date"])
            specials[d] = SpecialSession(
                date=d,
                name=str(item["name"]),
                preopen_start=_as_time(item["preopen_start"]),
                market_open=_as_time(item["market_open"]),
                market_close=_as_time(item["market_close"]),
                cas_continuous_end=_as_time(item["cas_continuous_end"]) if item.get("cas_continuous_end") else None,
            )
        years = {int(y) for y in raw.get("years_covered", [])}
        return cls(holidays, specials, years, settings)

    # ------------------------------------------------------------------ queries
    def _check_year(self, d: date) -> None:
        if d.year in self.years_covered:
            return
        msg = (
            f"NSE calendar file does not cover {d.year}; weekdays are treated as sessions. "
            "Add that year's holidays to config/nse_calendar.yaml."
        )
        if self._fail_if_missing:
            raise CalendarYearMissing(msg)
        if d.year not in self._warned_years:
            log.warning(msg)
            self._warned_years.add(d.year)

    def is_trading_day(self, d: date) -> bool:
        if d in self.special_sessions:
            return True
        self._check_year(d)
        if d.weekday() >= 5:
            return False
        return d not in self.holidays

    def holiday_name(self, d: date) -> str | None:
        return self.holidays.get(d)

    def session(self, d: date) -> Session | None:
        if not self.is_trading_day(d):
            return None
        s = self._s
        cas_on = s.cas.enabled and s.cas.applies_to != "none" and d >= s.cas.effective_from
        special = self.special_sessions.get(d)
        if special:
            open_dt = at(d, special.market_open)
            close_dt = at(d, special.market_close)
            cas_end = at(d, special.cas_continuous_end) if (cas_on and special.cas_continuous_end) else None
            pre = at(d, special.preopen_start)
            entry_end = pre + (datetime.combine(d, s.preopen_order_entry_end) - datetime.combine(d, s.preopen_start))
            iep_final = pre + (datetime.combine(d, s.preopen_iep_final_after) - datetime.combine(d, s.preopen_start))
            auction_end = (
                close_dt + (datetime.combine(d, s.cas.auction_end) - datetime.combine(d, s.market_close))
                if cas_end
                else None
            )
            return Session(d, pre, entry_end, iep_final, open_dt, close_dt, cas_end, auction_end, special.name)
        return Session(
            date=d,
            preopen_start=at(d, s.preopen_start),
            preopen_order_entry_end=at(d, s.preopen_order_entry_end),
            preopen_iep_final_after=at(d, s.preopen_iep_final_after),
            market_open=at(d, s.market_open),
            market_close=at(d, s.market_close),
            cas_continuous_end=at(d, s.cas.continuous_end) if cas_on else None,
            cas_auction_end=at(d, s.cas.auction_end) if cas_on else None,
            special=None,
        )

    def is_cas_stock(self, d: date, is_fno: bool) -> bool:
        """Does the Closing Auction Session apply to this stock on this date?"""
        c = self._s.cas
        if not c.enabled or d < c.effective_from or c.applies_to == "none":
            return False
        return c.applies_to == "all" or is_fno

    def next_trading_day(self, d: date, n: int = 1) -> date:
        cur = d
        count = 0
        while count < n:
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                count += 1
        return cur

    def previous_trading_day(self, d: date, n: int = 1) -> date:
        cur = d
        count = 0
        while count < n:
            cur -= timedelta(days=1)
            if self.is_trading_day(cur):
                count += 1
        return cur

    def trading_days(self, start: date, end: date) -> list[date]:
        """All sessions in [start, end], inclusive."""
        out = []
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def settlement_date(self, trade_date: date, cycle_days: int = 1) -> date:
        """T+N settlement (NSE equities are T+1)."""
        return self.next_trading_day(trade_date, cycle_days)

    def last_completed_session(self, now: datetime) -> date:
        """The most recent session whose close has passed at ``now``."""
        t = to_ist(now).to_pydatetime()
        d = t.date()
        sess = self.session(d)
        if sess and t >= sess.market_close:
            return d
        return self.previous_trading_day(d)


def _as_date(v: date | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def _as_time(v: time | str) -> time:
    if isinstance(v, time):
        return v
    hh, mm = str(v).split(":")[:2]
    return time(int(hh), int(mm))


__all__ = ["IST", "NSECalendar", "Session", "SpecialSession", "CalendarYearMissing"]
