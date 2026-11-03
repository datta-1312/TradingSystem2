"""NSE public reference files: downloader and tolerant parsers.

Sources (URLs live in settings.yaml → nse.* / universe.* / screener.themes.*):

* index constituents CSV (``ind_nifty500list.csv`` and thematic indices):
  ``Company Name, Industry, Symbol, Series, ISIN Code``
* ``sec_list.csv``: price band per symbol/series (``Band`` = 2, 5, 10, 20 or "No Band")
* ``fo_mktlots.csv``: F&O underlyings (no fixed band; Closing Auction Session applies)
* ASM / GSM surveillance reports (JSON from www.nseindia.com/api)
* corporate actions (JSON), trading holidays (JSON), pre-open market data (JSON)

www.nseindia.com/api endpoints need the cookies set by the home page, so the
client visits it first. The parsers accept small variations in column names
because NSE changes its file layouts from time to time. [VERIFY] the formats
against live files: the build sandbox could not reach NSE.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import requests

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings


class NseError(RuntimeError):
    pass


class NseClient:
    def __init__(self, settings: "Settings", session: requests.Session | Any | None = None):
        self.cfg = settings.nse
        self.http = session or requests.Session()
        self.http.headers.update(
            {"User-Agent": self.cfg.user_agent, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": self.cfg.home_url}
        )
        self._warmed = False

    def _warm(self) -> None:
        if not self._warmed:
            self.http.get(self.cfg.home_url, timeout=self.cfg.timeout_seconds)
            self._warmed = True

    def get(self, url: str) -> bytes:
        if "/api/" in url:
            self._warm()
        resp = self.http.get(url, timeout=self.cfg.timeout_seconds)
        if resp.status_code != 200:
            raise NseError(f"GET {url}: HTTP {resp.status_code}")
        return resp.content

    def get_json(self, url: str) -> Any:
        return json.loads(self.get(url).decode("utf-8"))

    def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.get(url))
        return dest


# ------------------------------------------------------------------ helpers
def _read_csv(src: str | Path | bytes) -> pd.DataFrame:
    if isinstance(src, bytes):
        df = pd.read_csv(io.BytesIO(src), dtype=str)
    elif isinstance(src, Path) or (isinstance(src, str) and "\n" not in src and Path(src).exists()):
        df = pd.read_csv(src, dtype=str)
    else:
        df = pd.read_csv(io.StringIO(str(src)), dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    return df.apply(lambda col: col.str.strip() if col.dtype == object or str(col.dtype) == "str" else col)


def _col(df: pd.DataFrame, *names: str) -> str | None:
    lower = {c.lower().replace(" ", "").replace("_", ""): c for c in df.columns}
    for n in names:
        key = n.lower().replace(" ", "").replace("_", "")
        if key in lower:
            return lower[key]
    return None


# ------------------------------------------------------------------ parsers
def parse_index_constituents(src: str | Path | bytes) -> pd.DataFrame:
    """→ DataFrame[symbol, name, industry, series, isin]."""
    df = _read_csv(src)
    sym = _col(df, "Symbol")
    if sym is None:
        raise NseError("constituent file has no Symbol column")
    out = pd.DataFrame(
        {
            "symbol": df[sym],
            "name": df[_col(df, "Company Name", "Name") or sym],
            "industry": df[_col(df, "Industry", "Sector")] if _col(df, "Industry", "Sector") else None,
            "series": df[_col(df, "Series")] if _col(df, "Series") else "EQ",
            "isin": df[_col(df, "ISIN Code", "ISIN")] if _col(df, "ISIN Code", "ISIN") else None,
        }
    )
    return out.dropna(subset=["symbol"]).reset_index(drop=True)


def parse_sec_list(src: str | Path | bytes) -> pd.DataFrame:
    """→ DataFrame[symbol, series, name, band_pct] with band_pct NaN for "No Band" (F&O)."""
    df = _read_csv(src)
    sym, series, band = _col(df, "Symbol"), _col(df, "Series"), _col(df, "Band")
    if sym is None or band is None:
        raise NseError("sec_list has no Symbol/Band column")

    def band_value(v: Any) -> float:
        text = str(v).strip().lower()
        if text in {"no band", "nan", "", "none"}:
            return float("nan")
        try:
            return float(text.rstrip("%"))
        except ValueError:
            return float("nan")

    return pd.DataFrame(
        {
            "symbol": df[sym],
            "series": df[series] if series else "EQ",
            "name": df[_col(df, "Security Name", "Name")] if _col(df, "Security Name", "Name") else df[sym],
            "band_pct": df[band].map(band_value),
            "band_raw": df[band],
        }
    )


_INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}


def parse_fo_lots(src: str | Path | bytes) -> set[str]:
    """F&O underlying stock symbols from fo_mktlots.csv (index underlyings removed)."""
    df = _read_csv(src)
    sym = _col(df, "SYMBOL", "Symbol")
    if sym is None:
        raise NseError("fo_mktlots has no SYMBOL column")
    syms = {s for s in df[sym].dropna().astype(str).str.strip() if s and s.upper() != "SYMBOL"}
    return {s for s in syms if s.upper() not in _INDEX_UNDERLYINGS}


def _walk_records(node: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], dict]]:
    found = []
    if isinstance(node, dict):
        if "symbol" in {k.lower() for k in node}:
            found.append((path, node))
        else:
            for k, v in node.items():
                found.extend(_walk_records(v, path + (str(k),)))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_records(item, path))
    return found


def _stage(rec: dict) -> str | None:
    for k, v in rec.items():
        kl = k.lower()
        if any(s in kl for s in ("stage", "indicator", "survdesc", "survcode")) and v not in (None, "", "-"):
            return str(v)
    return None


def parse_surveillance(payload: Any, framework: str) -> dict[str, str]:
    """ASM / GSM report JSON → {SYMBOL: "LT-ASM Stage I"} (tolerant of layout changes)."""
    out: dict[str, str] = {}
    for path, rec in _walk_records(payload):
        sym = next((str(v).strip() for k, v in rec.items() if k.lower() == "symbol"), "")
        if not sym:
            continue
        where = " ".join(p for p in path if p.lower() not in {"data"})
        prefix = framework
        if "long" in where.lower():
            prefix = f"LT-{framework}"
        elif "short" in where.lower():
            prefix = f"ST-{framework}"
        stage = _stage(rec)
        out[sym] = f"{prefix} {stage}".strip() if stage else prefix
    return out


def parse_holidays(payload: dict[str, Any], segment: str = "CM") -> list[tuple[date, str]]:
    rows = payload.get(segment) or []
    out = []
    for r in rows:
        raw = r.get("tradingDate") or r.get("date")
        if not raw:
            continue
        out.append((datetime.strptime(str(raw).strip(), "%d-%b-%Y").date(), str(r.get("description", "")).strip()))
    return out


@dataclass(frozen=True)
class PreopenInfo:
    symbol: str
    iep: float
    prev_close: float | None
    change_pct: float | None


def parse_preopen(payload: dict[str, Any]) -> dict[str, PreopenInfo]:
    out: dict[str, PreopenInfo] = {}
    for item in payload.get("data") or []:
        meta = item.get("metadata", item)
        sym = meta.get("symbol")
        iep = meta.get("iep") if meta.get("iep") not in (None, 0, "-") else meta.get("lastPrice")
        if not sym or iep in (None, "-"):
            continue
        out[str(sym)] = PreopenInfo(
            str(sym),
            float(iep),
            float(meta["previousClose"]) if meta.get("previousClose") not in (None, "-") else None,
            float(meta["pChange"]) if meta.get("pChange") not in (None, "-") else None,
        )
    return out
