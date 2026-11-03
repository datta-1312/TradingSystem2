"""Local CSV storage and an offline data provider.

Layout under the root directory::

    daily/<SYMBOL>.csv                         date,open,high,low,close,volume
    intraday/<SYMBOL>/<YYYY-MM-DD>_1min.csv    datetime,open,high,low,close,volume  (ISO, +05:30)
    quotes.csv  (optional)                     symbol,ltp,prev_close,upper_circuit,lower_circuit

The CSV provider powers the replay mode, the tests and the demo, and lets you
work offline with data you exported yourself.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from nseswing.data.base import Quote, normalize_daily, normalize_intraday


class CsvStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ------------------------------------------------------------------ paths
    def daily_path(self, symbol: str) -> Path:
        return self.root / "daily" / f"{symbol}.csv"

    def intraday_path(self, symbol: str, day: date, interval_minutes: int = 1) -> Path:
        return self.root / "intraday" / symbol / f"{day.isoformat()}_{interval_minutes}min.csv"

    # ------------------------------------------------------------------ daily
    def read_daily(self, symbol: str) -> pd.DataFrame | None:
        p = self.daily_path(symbol)
        if not p.exists():
            return None
        df = pd.read_csv(p, parse_dates=["date"], index_col="date")
        return normalize_daily(df)

    def write_daily(self, symbol: str, df: pd.DataFrame) -> Path:
        p = self.daily_path(symbol)
        p.parent.mkdir(parents=True, exist_ok=True)
        out = normalize_daily(df).copy()
        out.index = out.index.strftime("%Y-%m-%d")
        out.to_csv(p, index_label="date")
        return p

    def daily_symbols(self) -> list[str]:
        d = self.root / "daily"
        return sorted(p.stem for p in d.glob("*.csv")) if d.exists() else []

    # ------------------------------------------------------------------ intraday
    def read_intraday(self, symbol: str, day: date, interval_minutes: int = 1) -> pd.DataFrame | None:
        p = self.intraday_path(symbol, day, interval_minutes)
        if not p.exists():
            return None
        df = pd.read_csv(p)
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("datetime"), utc=True)).tz_convert("Asia/Kolkata")
        return normalize_intraday(df)

    def write_intraday(self, symbol: str, day: date, df: pd.DataFrame, interval_minutes: int = 1) -> Path:
        p = self.intraday_path(symbol, day, interval_minutes)
        p.parent.mkdir(parents=True, exist_ok=True)
        out = normalize_intraday(df).copy()
        out.index = [ts.isoformat() for ts in out.index]
        out.to_csv(p, index_label="datetime")
        return p

    def intraday_days(self, symbol: str, interval_minutes: int = 1) -> list[date]:
        d = self.root / "intraday" / symbol
        if not d.exists():
            return []
        suffix = f"_{interval_minutes}min.csv"
        return sorted(date.fromisoformat(p.name[: -len(suffix)]) for p in d.glob(f"*{suffix}"))

    # ------------------------------------------------------------------ quotes
    def read_quotes(self) -> dict[str, Quote]:
        p = self.root / "quotes.csv"
        if not p.exists():
            return {}
        df = pd.read_csv(p)
        out = {}
        for row in df.to_dict("records"):
            out[str(row["symbol"])] = Quote(
                symbol=str(row["symbol"]),
                ltp=float(row["ltp"]),
                prev_close=_opt(row.get("prev_close")),
                upper_circuit=_opt(row.get("upper_circuit")),
                lower_circuit=_opt(row.get("lower_circuit")),
            )
        return out


def _opt(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


class CsvProvider:
    """Offline provider over a :class:`CsvStore`."""

    name = "csv"

    def __init__(self, store: CsvStore):
        self.store = store

    def daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        df = self.store.read_daily(symbol)
        if df is None:
            return normalize_daily(pd.DataFrame())
        return df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]

    def intraday(self, symbol: str, day: date, interval_minutes: int = 1) -> pd.DataFrame:
        df = self.store.read_intraday(symbol, day, interval_minutes)
        if df is None and interval_minutes != 1:
            base = self.store.read_intraday(symbol, day, 1)
            if base is not None:
                from nseswing.intraday.bars import resample_bars

                return resample_bars(base, interval_minutes)
        return df if df is not None else normalize_intraday(pd.DataFrame())

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        stored = self.store.read_quotes()
        out: dict[str, Quote] = {}
        for sym in symbols:
            if sym in stored:
                out[sym] = stored[sym]
                continue
            df = self.store.read_daily(sym)
            if df is None or df.empty:
                continue
            last = df.iloc[-1]
            prev = float(df["close"].iloc[-2]) if len(df) > 1 else None
            out[sym] = Quote(sym, float(last["close"]), prev, float(last["open"]), float(last["high"]), float(last["low"]), float(last["volume"]))
        return out
