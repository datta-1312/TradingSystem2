import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from nseswing.config import load_settings
from nseswing.core.timeutil import IST
from nseswing.corpactions import parse_nse_actions_json
from nseswing.data import nse
from nseswing.data.csvstore import CsvProvider, CsvStore
from nseswing.data.reference import ReferenceData
from nseswing.data.upstox import RateLimiter, UpstoxClient, UpstoxError, UpstoxProvider
from nseswing.intraday.bars import BarBuilder, resample_bars

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


# ------------------------------------------------------------------ Upstox parsing
def test_parse_daily_candles_sorted_naive_dates():
    df = UpstoxClient.parse_candles(load("upstox_daily_candles.json"), intraday=False)
    assert list(df.index) == [pd.Timestamp("2026-10-28"), pd.Timestamp("2026-10-29"), pd.Timestamp("2026-10-30")]
    assert df.index.tz is None
    assert df.loc["2026-10-30", "close"] == 517.35 and df["volume"].dtype == float


def test_parse_intraday_candles_ist():
    df = UpstoxClient.parse_candles(load("upstox_intraday_candles.json"), intraday=True)
    assert str(df.index.tz) == "Asia/Kolkata"
    assert df.index[0] == pd.Timestamp("2026-10-30 09:15", tz=IST)


def test_parse_full_quote_circuits_and_prev_close():
    q = UpstoxClient.parse_full_quotes(load("upstox_full_quote.json"))["NSE_EQ|INE000A01010"]
    assert q.symbol == "ALPHAIND"
    assert q.prev_close == pytest.approx(511.9)
    assert (q.lower_circuit, q.upper_circuit) == (460.75, 563.05)
    assert q.timestamp.tzinfo is not None


def test_parse_ltp_and_instruments():
    ltp = UpstoxClient.parse_ltp(load("upstox_ltp.json"))
    assert ltp["NSE_EQ|INE000A01010"] == (517.35, 1834000.0)
    inst = UpstoxClient.parse_instruments(load("upstox_instruments_sample.json"))
    assert set(inst) == {"ALPHAIND", "GAMMADEF"}
    assert inst["ALPHAIND"].instrument_key == "NSE_EQ|INE000A01010"


# ------------------------------------------------------------------ Upstox transport
class FakeResponse:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, payload):
        self.payload, self.calls = payload, []
        self.headers = {}

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        return FakeResponse(self.payload)

    def get(self, url, timeout=None):
        self.calls.append({"method": "GET", "url": url})
        return FakeResponse(self.payload)


def test_historical_requests_are_chunked_and_encoded(settings):
    sess = FakeSession(load("upstox_intraday_candles.json"))
    client = UpstoxClient(settings, session=sess, token="T0KEN")
    client.historical_candles("NSE_EQ|INE000A01010", "minutes", 1, date(2026, 8, 1), date(2026, 10, 9))
    urls = [c["url"] for c in sess.calls]
    assert len(urls) == 3  # 70 days at 28 days per request
    assert urls[0].endswith("/v3/historical-candle/NSE_EQ%7CINE000A01010/minutes/1/2026-08-28/2026-08-01")
    assert sess.calls[0]["headers"]["Authorization"] == "Bearer T0KEN"


def test_algo_header_added_when_configured():
    s = load_settings(overrides={"execution": {"live": {"algo_header_name": "X-Algo-Name", "algo_header_value": "my-swing-algo"}}})
    client = UpstoxClient(s, session=FakeSession({"status": "success", "data": {}}), token="T")
    assert client.headers()["X-Algo-Name"] == "my-swing-algo"


def test_error_status_raises(settings):
    sess = FakeSession({"status": "error", "errors": [{"message": "Invalid token"}]})
    client = UpstoxClient(settings, session=sess, token="T")
    with pytest.raises(UpstoxError):
        client.request("GET", "/v2/user/profile")


def test_token_file_expires_next_morning(tmp_path, monkeypatch):
    s = load_settings(overrides={"upstox": {"token_file": str(tmp_path / "tok.json")}})
    monkeypatch.delenv(s.upstox.access_token_env, raising=False)
    client = UpstoxClient(s)
    obtained = datetime(2026, 11, 3, 8, 30, tzinfo=IST)
    client.save_token("ABC", obtained)
    assert UpstoxClient(s).load_token(now=obtained + timedelta(hours=10)) == "ABC"
    assert UpstoxClient(s).load_token(now=datetime(2026, 11, 4, 3, 31, tzinfo=IST)) is None


def test_rate_limiter_waits():
    t = [0.0]
    slept = []

    def sleep(dt):
        slept.append(dt)
        t[0] += dt

    rl = RateLimiter(per_second=2, per_minute=100, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        rl.acquire()
    assert slept and slept[0] == pytest.approx(1.0)


def test_provider_falls_back_to_isin_key(settings):
    ref = ReferenceData.build(settings, nse.parse_index_constituents(FIX / "nifty500_sample.csv"))
    prov = UpstoxProvider(settings, client=UpstoxClient(settings, token="x"), reference=ref)
    assert prov.instrument_key("BETAPOWER") == "NSE_EQ|INE000B01012"


# ------------------------------------------------------------------ NSE parsers
def test_nse_parsers():
    cons = nse.parse_index_constituents(FIX / "nifty500_sample.csv")
    assert list(cons.columns) == ["symbol", "name", "industry", "series", "isin"]
    sec = nse.parse_sec_list(FIX / "sec_list_sample.csv")
    assert pd.isna(sec.set_index("symbol").loc["ALPHAIND", "band_pct"])
    assert sec.set_index("symbol").loc["BETAPOWER", "band_pct"] == 20.0
    assert nse.parse_fo_lots(FIX / "fo_mktlots_sample.csv") == {"ALPHAIND", "GAMMADEF"}
    asm = nse.parse_surveillance(load("asm_sample.json"), "ASM")
    assert asm == {"DELTACHEM": "LT-ASM Stage I", "TTCO": "ST-ASM Stage II"}
    assert nse.parse_surveillance(load("gsm_sample.json"), "GSM") == {"PENNYCO": "GSM Stage 0"}
    hol = nse.parse_holidays(load("holidays_sample.json"))
    assert hol[0] == (date(2026, 1, 26), "Republic Day")
    pre = nse.parse_preopen(load("preopen_sample.json"))
    assert pre["ALPHAIND"].iep == 525.0
    assert pre["BETAPOWER"].iep == 210.5  # iep 0 → last price
    acts = parse_nse_actions_json(load("corp_actions_sample.json"))
    kinds = {(a.symbol, a.kind) for a in acts}
    assert kinds == {("BETAPOWER", "split"), ("DELTACHEM", "bonus"), ("ALPHAIND", "dividend")}


# ------------------------------------------------------------------ reference data
@pytest.fixture()
def reference(settings):
    return ReferenceData.build(
        settings,
        nse.parse_index_constituents(FIX / "nifty500_sample.csv"),
        sec_list=nse.parse_sec_list(FIX / "sec_list_sample.csv"),
        fo_symbols=nse.parse_fo_lots(FIX / "fo_mktlots_sample.csv"),
        asm=nse.parse_surveillance(load("asm_sample.json"), "ASM"),
        gsm={},
        index_themes={"NIFTY INDIA DEFENCE": {"GAMMADEF"}},
        custom_themes={"Power Capex": ["ALPHAIND", "BETAPOWER", "NOTLISTED"]},
        instruments=UpstoxClient.parse_instruments(load("upstox_instruments_sample.json")),
    )


def test_reference_flags(reference):
    a = reference.get("ALPHAIND")
    assert a.is_fno and a.band_pct is None and a.instrument_key == "NSE_EQ|INE000A01010"
    assert a.themes == ["Power Capex"]
    t = reference.get("TTCO")
    assert t.t2t and "ST-ASM Stage II" in t.surveillance_flags and "Trade-for-trade (BE)" in t.surveillance_flags
    assert reference.get("DELTACHEM").band_pct == 10.0
    groups = reference.groups()
    assert groups["Industry: Capital Goods"] == {"ALPHAIND", "BETAPOWER", "GAMMADEF"}
    assert groups["Theme: NIFTY INDIA DEFENCE"] == {"GAMMADEF"}


def test_circuit_limits(reference, settings):
    d = reference.get("DELTACHEM")
    assert d.band_pct == 10.0
    lim = reference.circuit_limits("DELTACHEM", 487.33, Decimal("0.05"), settings)
    assert (lim.lower, lim.upper, lim.source) == (Decimal("438.60"), Decimal("536.05"), "band")
    est = reference.circuit_limits("ALPHAIND", 500.0, Decimal("0.05"), settings)
    assert est.source == "fno-estimate" and est.upper == Decimal("550.00")
    q = UpstoxClient.parse_full_quotes(load("upstox_full_quote.json"))["NSE_EQ|INE000A01010"]
    assert reference.circuit_limits("ALPHAIND", 511.9, Decimal("0.05"), settings, quote=q).source == "quote"
    assert lim.proximity(531.0, 1.0) == ["NEAR UPPER CIRCUIT"]


def test_tick_from_instrument_master(reference, settings, ticks):
    assert reference.tick("ALPHAIND", 517.0, ticks, settings) == Decimal("0.05")  # file says 5.0 (paise)
    assert reference.tick("BETAPOWER", 210.0, ticks, settings) == Decimal("0.01")  # table


# ------------------------------------------------------------------ CSV store / bars
def test_csv_roundtrip_and_provider(tmp_path):
    store = CsvStore(tmp_path)
    daily = UpstoxClient.parse_candles(load("upstox_daily_candles.json"), intraday=False)
    store.write_daily("ALPHAIND", daily)
    intra = UpstoxClient.parse_candles(load("upstox_intraday_candles.json"), intraday=True)
    store.write_intraday("ALPHAIND", date(2026, 10, 30), intra)
    prov = CsvProvider(store)
    pd.testing.assert_frame_equal(prov.daily("ALPHAIND", date(2026, 1, 1), date(2026, 12, 31)), daily, check_freq=False)
    back = prov.intraday("ALPHAIND", date(2026, 10, 30), 1)
    assert back.index[0] == pd.Timestamp("2026-10-30 09:15", tz=IST)
    assert prov.intraday("ALPHAIND", date(2026, 10, 30), 5)["volume"].iloc[0] == 81500.0
    q = prov.quotes(["ALPHAIND"])["ALPHAIND"]
    assert q.ltp == 517.35 and q.prev_close == 511.9


def test_resample_and_bar_builder():
    idx = pd.date_range("2026-10-30 09:15", periods=12, freq="1min", tz=IST)
    df = pd.DataFrame({"open": range(12), "high": [x + 1 for x in range(12)], "low": range(12), "close": range(12), "volume": [10] * 12}, index=idx, dtype=float)
    five = resample_bars(df, 5)
    assert list(five.index.strftime("%H:%M")) == ["09:15", "09:20", "09:25"]
    assert five.iloc[0].to_dict() == {"open": 0.0, "high": 5.0, "low": 0.0, "close": 4.0, "volume": 50.0}

    bb = BarBuilder(5, datetime(2026, 10, 30, 9, 15, tzinfo=IST))
    assert bb.on_tick(datetime(2026, 10, 30, 9, 15, 5, tzinfo=IST), 500.0, 1000) == []
    bb.on_tick(datetime(2026, 10, 30, 9, 17, tzinfo=IST), 502.0, 1600)
    done = bb.on_tick(datetime(2026, 10, 30, 9, 20, 1, tzinfo=IST), 501.0, 1700)
    assert len(done) == 1 and (done[0].high, done[0].low, done[0].volume) == (502.0, 500.0, 600.0)
    assert bb.current.open == 501.0
