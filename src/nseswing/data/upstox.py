"""Upstox API client (REST v2/v3) and the Upstox data provider.

Endpoints (verify against https://upstox.com/developer/api-documentation before
going live; the build sandbox could not reach Upstox, so these were written
from the published formats and tested against payload fixtures):

    GET    /v2/login/authorization/dialog                       OAuth login page
    POST   /v2/login/authorization/token                        code -> access_token
    GET    /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}
    GET    /v3/historical-candle/intraday/{key}/{unit}/{interval}
    GET    /v2/market-quote/quotes?instrument_key=...           full quote incl. circuit limits
    GET    /v3/market-quote/ltp?instrument_key=...
    POST   /v2/order/place   (or https://api-hft.upstox.com/v3/order/place)
    DELETE /v2/order/cancel?order_id=...
    GET    /v2/order/details?order_id=...
    POST   /v3/order/gtt/place   PUT /v3/order/gtt/modify   DELETE /v3/order/gtt/cancel   GET /v3/order/gtt
    GET    /v2/portfolio/long-term-holdings

Instrument keys look like ``NSE_EQ|INE002A01018`` (segment | ISIN).
"""

from __future__ import annotations

import gzip
import json
import os
import time as _time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote, urlencode

import pandas as pd
import requests

from nseswing.core.timeutil import IST, now_ist
from nseswing.data.base import Quote, normalize_daily, normalize_intraday

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings
    from nseswing.data.csvstore import CsvStore
    from nseswing.data.reference import ReferenceData


class UpstoxError(RuntimeError):
    pass


class RateLimiter:
    """Sliding-window limiter for requests per second and per minute."""

    def __init__(
        self,
        per_second: float,
        per_minute: float,
        clock: Callable[[], float] = _time.monotonic,
        sleep: Callable[[float], None] = _time.sleep,
    ):
        self.per_second, self.per_minute = per_second, per_minute
        self._clock, self._sleep = clock, sleep
        self._stamps: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._stamps and now - self._stamps[0] >= 60.0:
                self._stamps.popleft()
            last_second = [t for t in self._stamps if now - t < 1.0]
            wait = 0.0
            if len(last_second) >= self.per_second:
                wait = max(wait, 1.0 - (now - last_second[0]))
            if len(self._stamps) >= self.per_minute:
                wait = max(wait, 60.0 - (now - self._stamps[0]))
            if wait <= 0:
                self._stamps.append(now)
                return
            self._sleep(wait)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    instrument_key: str
    isin: str | None
    name: str | None
    tick_size_raw: float | None
    lot_size: int | None
    instrument_type: str | None


class UpstoxClient:
    def __init__(self, settings: "Settings", session: requests.Session | Any | None = None, token: str | None = None):
        self.s = settings
        self.cfg = settings.upstox
        self.http = session or requests.Session()
        self._token = token
        self.limiter = RateLimiter(self.cfg.rate_limit_per_second, self.cfg.rate_limit_per_minute)

    # ------------------------------------------------------------------ auth
    def authorization_url(self, state: str = "nseswing") -> str:
        client_id = os.environ.get(self.cfg.client_id_env, "")
        redirect = os.environ.get(self.cfg.redirect_uri_env, "")
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "state": state}
        return f"{self.cfg.api_base}/v2/login/authorization/dialog?{urlencode(params)}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        """Swap the login ``code`` for today's access token and save it to the token file."""
        data = {
            "code": code,
            "client_id": os.environ.get(self.cfg.client_id_env, ""),
            "client_secret": os.environ.get(self.cfg.client_secret_env, ""),
            "redirect_uri": os.environ.get(self.cfg.redirect_uri_env, ""),
            "grant_type": "authorization_code",
        }
        resp = self.http.post(
            f"{self.cfg.api_base}/v2/login/authorization/token",
            data=data,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.cfg.timeout_seconds,
        )
        payload = _json_or_raise(resp)
        token = payload.get("access_token")
        if not token:
            raise UpstoxError(f"no access_token in response: {payload}")
        self._token = token
        self.save_token(token)
        return payload

    def save_token(self, token: str, obtained: datetime | None = None) -> Path:
        obtained = obtained or now_ist()
        # Upstox access tokens are valid until 03:30 IST the next day (daily login).
        expires = datetime.combine(obtained.date() + timedelta(days=1), datetime.min.time(), tzinfo=IST) + timedelta(hours=3, minutes=30)
        path = self.s.path(self.cfg.token_file)
        path.write_text(json.dumps({"access_token": token, "obtained_at": obtained.isoformat(), "expires_at": expires.isoformat()}))
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass
        return path

    def load_token(self, now: datetime | None = None) -> str | None:
        if self._token:
            return self._token
        env = os.environ.get(self.cfg.access_token_env)
        if env:
            return env
        path = self.s.path(self.cfg.token_file)
        if not path.exists():
            return None
        info = json.loads(path.read_text())
        if datetime.fromisoformat(info["expires_at"]) <= (now or now_ist()):
            return None
        return info.get("access_token")

    # ------------------------------------------------------------------ transport
    def headers(self) -> dict[str, str]:
        token = self.load_token()
        if not token:
            raise UpstoxError("no Upstox access token: run `nseswing upstox-login` (tokens expire daily)")
        h = {"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        live = self.s.execution.live
        if live.algo_header_name and live.algo_header_value:
            h[live.algo_header_name] = live.algo_header_value
        return h

    def request(
        self, method: str, path: str, *, base: str | None = None, params: dict | None = None, body: dict | None = None
    ) -> dict[str, Any]:
        self.limiter.acquire()
        url = f"{base or self.cfg.api_base}{path}"
        resp = self.http.request(method, url, params=params, json=body, headers=self.headers(), timeout=self.cfg.timeout_seconds)
        payload = _json_or_raise(resp)
        if payload.get("status") not in (None, "success"):
            raise UpstoxError(f"{method} {path}: {payload}")
        return payload

    # ------------------------------------------------------------------ instruments
    def download_instruments(self, dest: Path) -> Path:
        resp = self.http.get(self.cfg.instruments_url, timeout=self.cfg.timeout_seconds * 4)
        if resp.status_code != 200:
            raise UpstoxError(f"instrument download failed: HTTP {resp.status_code}")
        raw = resp.content
        data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    @staticmethod
    def parse_instruments(records: list[dict[str, Any]]) -> dict[str, Instrument]:
        """Map NSE trading symbol → instrument (cash-segment rows only, EQ preferred)."""
        out: dict[str, Instrument] = {}
        for r in records:
            if r.get("segment") != "NSE_EQ":
                continue
            sym = r.get("trading_symbol") or r.get("tradingsymbol")
            key = r.get("instrument_key")
            if not sym or not key:
                continue
            inst = Instrument(
                symbol=str(sym),
                instrument_key=str(key),
                isin=r.get("isin"),
                name=r.get("name"),
                tick_size_raw=float(r["tick_size"]) if r.get("tick_size") is not None else None,
                lot_size=int(r["lot_size"]) if r.get("lot_size") is not None else None,
                instrument_type=r.get("instrument_type"),
            )
            existing = out.get(inst.symbol)
            if existing is None or (existing.instrument_type != "EQ" and inst.instrument_type == "EQ"):
                out[inst.symbol] = inst
        return out

    @classmethod
    def load_instruments(cls, path: Path) -> dict[str, Instrument]:
        return cls.parse_instruments(json.loads(path.read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ candles
    @staticmethod
    def parse_candles(payload: dict[str, Any], intraday: bool) -> pd.DataFrame:
        candles = (payload.get("data") or {}).get("candles") or []
        if not candles:
            return normalize_intraday(pd.DataFrame()) if intraday else normalize_daily(pd.DataFrame())
        df = pd.DataFrame([c[:6] for c in candles], columns=["ts", "open", "high", "low", "close", "volume"])
        idx = pd.to_datetime(df.pop("ts"), utc=True).dt.tz_convert(IST)
        df.index = pd.DatetimeIndex(idx)
        return normalize_intraday(df) if intraday else normalize_daily(df)

    def historical_candles(self, instrument_key: str, unit: str, interval: int, start: date, end: date) -> pd.DataFrame:
        """v3 historical candles, split into allowed date ranges per request."""
        intraday = unit in ("minutes", "hours")
        span = self.cfg.intraday_max_days_per_request if intraday else self.cfg.daily_max_days_per_request
        frames = []
        chunk_start = start
        key = quote(instrument_key, safe="")
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=span - 1))
            path = f"/v3/historical-candle/{key}/{unit}/{interval}/{chunk_end.isoformat()}/{chunk_start.isoformat()}"
            frames.append(self.parse_candles(self.request("GET", path), intraday))
            chunk_start = chunk_end + timedelta(days=1)
        frames = [f for f in frames if not f.empty]
        if not frames:
            return self.parse_candles({}, intraday)
        df = pd.concat(frames)
        return normalize_intraday(df) if intraday else normalize_daily(df)

    def intraday_candles(self, instrument_key: str, interval: int = 1) -> pd.DataFrame:
        """Today's candles so far (v3 intraday endpoint)."""
        key = quote(instrument_key, safe="")
        return self.parse_candles(self.request("GET", f"/v3/historical-candle/intraday/{key}/minutes/{interval}"), True)

    # ------------------------------------------------------------------ quotes
    @staticmethod
    def parse_full_quotes(payload: dict[str, Any]) -> dict[str, Quote]:
        """Keyed by instrument key. prev_close = last_price − net_change."""
        out: dict[str, Quote] = {}
        for label, q in (payload.get("data") or {}).items():
            key = q.get("instrument_token") or label
            ltp = float(q.get("last_price") or 0.0)
            ohlc = q.get("ohlc") or {}
            net = q.get("net_change")
            prev = ltp - float(net) if net is not None else (float(ohlc["close"]) if ohlc.get("close") else None)
            ts = q.get("timestamp") or q.get("last_trade_time")
            out[key] = Quote(
                symbol=str(q.get("symbol") or label.split(":")[-1]),
                ltp=ltp,
                prev_close=prev,
                open=_f(ohlc.get("open")),
                high=_f(ohlc.get("high")),
                low=_f(ohlc.get("low")),
                volume=_f(q.get("volume")),
                upper_circuit=_f(q.get("upper_circuit_limit")),
                lower_circuit=_f(q.get("lower_circuit_limit")),
                timestamp=_ts(ts),
                instrument_key=key,
            )
        return out

    def full_quotes(self, instrument_keys: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        size = self.cfg.quote_batch_size
        for i in range(0, len(instrument_keys), size):
            batch = instrument_keys[i : i + size]
            out.update(self.parse_full_quotes(self.request("GET", "/v2/market-quote/quotes", params={"instrument_key": ",".join(batch)})))
        return out

    @staticmethod
    def parse_ltp(payload: dict[str, Any]) -> dict[str, tuple[float, float | None]]:
        """Keyed by instrument key → (last price, cumulative day volume)."""
        out = {}
        for label, q in (payload.get("data") or {}).items():
            key = q.get("instrument_token") or label
            out[key] = (float(q.get("last_price") or 0.0), _f(q.get("volume")))
        return out

    def ltp(self, instrument_keys: list[str]) -> dict[str, tuple[float, float | None]]:
        out: dict[str, tuple[float, float | None]] = {}
        size = self.cfg.quote_batch_size
        for i in range(0, len(instrument_keys), size):
            batch = instrument_keys[i : i + size]
            out.update(self.parse_ltp(self.request("GET", "/v3/market-quote/ltp", params={"instrument_key": ",".join(batch)})))
        return out

    # ------------------------------------------------------------------ orders (used only by the live gateway)
    def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.s.execution.live.use_hft_endpoint:
            return self.request("POST", "/v3/order/place", base=self.cfg.hft_base, body=body)
        return self.request("POST", "/v2/order/place", body=body)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self.request("DELETE", "/v2/order/cancel", params={"order_id": order_id})

    def order_details(self, order_id: str) -> dict[str, Any]:
        return self.request("GET", "/v2/order/details", params={"order_id": order_id})

    def place_gtt(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/v3/order/gtt/place", body=body)

    def modify_gtt(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", "/v3/order/gtt/modify", body=body)

    def cancel_gtt(self, gtt_order_id: str) -> dict[str, Any]:
        return self.request("DELETE", "/v3/order/gtt/cancel", body={"gtt_order_id": gtt_order_id})

    def gtt_details(self, gtt_order_id: str) -> dict[str, Any]:
        return self.request("GET", "/v3/order/gtt", params={"gtt_order_id": gtt_order_id})

    def holdings(self) -> dict[str, Any]:
        return self.request("GET", "/v2/portfolio/long-term-holdings")


class UpstoxProvider:
    """DataProvider backed by Upstox, with every download mirrored to the CSV cache."""

    name = "upstox"

    def __init__(
        self,
        settings: "Settings",
        client: UpstoxClient | None = None,
        instruments: dict[str, Instrument] | None = None,
        reference: "ReferenceData | None" = None,
        cache: "CsvStore | None" = None,
    ):
        self.s = settings
        self.client = client or UpstoxClient(settings)
        self.instruments = instruments or {}
        self.reference = reference
        self.cache = cache

    def instrument_key(self, symbol: str) -> str:
        if symbol in self.instruments:
            return self.instruments[symbol].instrument_key
        if self.reference is not None:
            info = self.reference.get(symbol)
            if info and info.isin:
                return f"NSE_EQ|{info.isin}"
        raise UpstoxError(f"no Upstox instrument key for {symbol}: refresh the instrument master")

    def daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        df = self.client.historical_candles(self.instrument_key(symbol), "days", 1, start, end)
        if self.cache is not None and not df.empty:
            self.cache.write_daily(symbol, df)
        return df

    def intraday(self, symbol: str, day: date, interval_minutes: int = 1) -> pd.DataFrame:
        key = self.instrument_key(symbol)
        if day == now_ist().date():
            df = self.client.intraday_candles(key, interval_minutes)
        else:
            df = self.client.historical_candles(key, "minutes", interval_minutes, day, day)
        if self.cache is not None and not df.empty:
            self.cache.write_intraday(symbol, day, df, interval_minutes)
        return df

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        keys = {self.instrument_key(s): s for s in symbols}
        by_key = self.client.full_quotes(list(keys))
        return {keys[k]: q for k, q in by_key.items() if k in keys}

    def ltp(self, symbols: list[str]) -> dict[str, tuple[float, float | None]]:
        keys = {self.instrument_key(s): s for s in symbols}
        return {keys[k]: v for k, v in self.client.ltp(list(keys)).items() if k in keys}


def _json_or_raise(resp: Any) -> dict[str, Any]:
    try:
        payload = resp.json()
    except ValueError as exc:
        raise UpstoxError(f"HTTP {resp.status_code}: non-JSON response") from exc
    if resp.status_code >= 400:
        raise UpstoxError(f"HTTP {resp.status_code}: {payload}")
    return payload


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> pd.Timestamp | None:
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            return pd.Timestamp(int(v), unit="ms", tz="UTC").tz_convert(IST)
        stamp = pd.Timestamp(v)
        return stamp.tz_localize(IST) if stamp.tzinfo is None else stamp.tz_convert(IST)
    except (ValueError, TypeError):
        return None
