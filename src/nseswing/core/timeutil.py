"""IST time helpers. Every timestamp in the system is timezone-aware Asia/Kolkata."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def at(d: date, t: time) -> datetime:
    """Combine an NSE trading date and an IST wall-clock time."""
    return datetime.combine(d, t, tzinfo=IST)


def to_ist(ts: datetime | pd.Timestamp | str) -> pd.Timestamp:
    """Return a tz-aware IST pandas Timestamp (naive input is assumed to be IST)."""
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        return stamp.tz_localize(IST)
    return stamp.tz_convert(IST)


def ensure_ist_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if index.tz is None:
        return index.tz_localize(IST)
    return index.tz_convert(IST)


def fmt_ist(ts: datetime | pd.Timestamp, with_date: bool = True) -> str:
    stamp = to_ist(ts)
    return stamp.strftime("%Y-%m-%d %H:%M IST" if with_date else "%H:%M")
