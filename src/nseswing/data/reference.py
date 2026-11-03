"""Per-symbol reference data: universe, industry, themes, price band, F&O flag,
series, surveillance flags and tick.

Everything is assembled from files in ``data.reference_dir`` (downloaded by
``nseswing refresh-reference``) plus the user files ``config/themes.yaml`` and
``surveillance.manual_flags``. Missing files degrade gracefully: the affected
fields stay empty and a note is recorded so the screener can say why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import yaml

from nseswing.core.ticks import TickTable, round_to_tick, to_decimal
from nseswing.data import nse

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings
    from nseswing.data.base import Quote
    from nseswing.data.upstox import Instrument


@dataclass
class SecurityInfo:
    symbol: str
    name: str = ""
    isin: str | None = None
    industry: str | None = None
    themes: list[str] = field(default_factory=list)
    series: str = "EQ"
    band_pct: float | None = None  # None = no fixed band (F&O stocks)
    band_source: str = "unknown"
    is_fno: bool = False
    asm: str | None = None
    gsm: str | None = None
    t2t: bool = False
    manual_flag: str | None = None
    instrument_key: str | None = None
    tick_size_raw: float | None = None

    @property
    def surveillance_flags(self) -> list[str]:
        flags = []
        if self.asm:
            flags.append(self.asm)
        if self.gsm:
            flags.append(self.gsm)
        if self.t2t:
            flags.append(f"Trade-for-trade ({self.series})")
        if self.manual_flag:
            flags.append(f"Manual: {self.manual_flag}")
        return flags

    @property
    def groups(self) -> list[str]:
        out = [f"Industry: {self.industry}"] if self.industry else []
        return out + [f"Theme: {t}" for t in self.themes]


@dataclass
class CircuitLimits:
    lower: Decimal
    upper: Decimal
    band_pct: float | None
    source: str  # "quote" | "band" | "fno-estimate" | "default-band"

    def proximity(self, price: float, near_pct: float) -> list[str]:
        p = float(price)
        flags = []
        if p >= float(self.upper):
            flags.append("AT UPPER CIRCUIT")
        elif (float(self.upper) - p) / p * 100 <= near_pct:
            flags.append("NEAR UPPER CIRCUIT")
        if p <= float(self.lower):
            flags.append("AT LOWER CIRCUIT")
        elif (p - float(self.lower)) / p * 100 <= near_pct:
            flags.append("NEAR LOWER CIRCUIT")
        return flags


class ReferenceData:
    def __init__(self, securities: dict[str, SecurityInfo], notes: list[str] | None = None):
        self.securities = securities
        self.notes = notes or []

    # ------------------------------------------------------------------ construction
    @classmethod
    def build(
        cls,
        settings: "Settings",
        constituents: pd.DataFrame,
        sec_list: pd.DataFrame | None = None,
        fo_symbols: set[str] | None = None,
        asm: dict[str, str] | None = None,
        gsm: dict[str, str] | None = None,
        index_themes: dict[str, set[str]] | None = None,
        custom_themes: dict[str, list[str]] | None = None,
        instruments: "dict[str, Instrument] | None" = None,
    ) -> "ReferenceData":
        u, sv = settings.universe, settings.surveillance
        notes: list[str] = []
        rows = constituents.copy()
        extra = [s for s in u.extra_symbols if s not in set(rows["symbol"])]
        if extra:
            rows = pd.concat([rows, pd.DataFrame({"symbol": extra, "name": extra, "industry": None, "series": "EQ", "isin": None})])
        rows = rows[~rows["symbol"].isin(u.exclude_symbols)]
        bands = {}
        if sec_list is not None and not sec_list.empty:
            eq_first = sec_list.assign(_o=(sec_list["series"] != "EQ").astype(int)).sort_values("_o")
            bands = {r["symbol"]: (r["series"], r["band_pct"]) for r in eq_first.drop_duplicates("symbol").to_dict("records")}
        else:
            notes.append("sec_list.csv not loaded: price bands use defaults")
        if fo_symbols is None:
            notes.append("F&O list not loaded: CAS / no-band status unknown")
        theme_members: dict[str, set[str]] = {}
        for name, members in (index_themes or {}).items():
            theme_members[name] = set(members)
        for name, members in (custom_themes or {}).items():
            theme_members[name] = theme_members.get(name, set()) | set(members)

        securities: dict[str, SecurityInfo] = {}
        for r in rows.to_dict("records"):
            sym = str(r["symbol"])
            series = str(r.get("series") or "EQ")
            band_pct: float | None
            band_source = "default-band"
            band_pct = settings.price_bands.default_band_pct
            if sym in bands:
                series_from_list, b = bands[sym]
                series = series_from_list or series
                if pd.isna(b):
                    band_pct, band_source = None, "no-band (F&O)"
                else:
                    band_pct, band_source = float(b), "sec_list"
            is_fno = bool(fo_symbols and sym in fo_symbols) or band_source == "no-band (F&O)"
            if is_fno and band_source != "sec_list":
                band_pct, band_source = None, "no-band (F&O)"
            if series not in u.series_allowed:
                continue
            inst = (instruments or {}).get(sym)
            isin = r.get("isin") if isinstance(r.get("isin"), str) else None
            securities[sym] = SecurityInfo(
                symbol=sym,
                name=str(r.get("name") or sym),
                isin=isin or (inst.isin if inst else None),
                industry=r.get("industry") if isinstance(r.get("industry"), str) else None,
                themes=sorted(t for t, m in theme_members.items() if sym in m),
                series=series,
                band_pct=band_pct,
                band_source=band_source,
                is_fno=is_fno,
                asm=(asm or {}).get(sym),
                gsm=(gsm or {}).get(sym),
                t2t=series in sv.t2t_series,
                manual_flag=sv.manual_flags.get(sym),
                instrument_key=inst.instrument_key if inst else (f"NSE_EQ|{isin}" if isin else None),
                tick_size_raw=inst.tick_size_raw if inst else None,
            )
        return cls(securities, notes)

    @classmethod
    def load(cls, settings: "Settings") -> "ReferenceData":
        """Assemble from whatever reference files exist on disk."""
        ref_dir = settings.path(settings.data.reference_dir)
        constituents_file = settings.path(settings.universe.constituents_file)
        if not constituents_file.exists():
            raise FileNotFoundError(
                f"{constituents_file} not found: run `nseswing refresh-reference` or place the Nifty 500 CSV there"
            )
        constituents = nse.parse_index_constituents(constituents_file)
        sec_file = settings.path(settings.price_bands.sec_list_file)
        fo_file = settings.path(settings.price_bands.fo_list_file)
        asm_file, gsm_file = settings.path(settings.surveillance.asm_file), settings.path(settings.surveillance.gsm_file)
        index_themes = {}
        for name in settings.screener.themes.index_themes:
            p = ref_dir / "themes" / f"{_slug(name)}.csv"
            if p.exists():
                index_themes[name] = set(nse.parse_index_constituents(p)["symbol"])
        instruments = None
        inst_file = ref_dir / "upstox_instruments_nse.json"
        if inst_file.exists():
            from nseswing.data.upstox import UpstoxClient

            instruments = UpstoxClient.load_instruments(inst_file)
        return cls.build(
            settings,
            constituents,
            sec_list=nse.parse_sec_list(sec_file) if sec_file.exists() else None,
            fo_symbols=nse.parse_fo_lots(fo_file) if fo_file.exists() else None,
            asm=nse.parse_surveillance(json.loads(asm_file.read_text()), "ASM") if asm_file.exists() else None,
            gsm=nse.parse_surveillance(json.loads(gsm_file.read_text()), "GSM") if gsm_file.exists() else None,
            index_themes=index_themes,
            custom_themes=load_custom_themes(settings.path(settings.screener.themes.custom_themes_file)),
            instruments=instruments,
        )

    # ------------------------------------------------------------------ queries
    def get(self, symbol: str) -> SecurityInfo | None:
        return self.securities.get(symbol)

    def symbols(self) -> list[str]:
        return sorted(self.securities)

    def groups(self, use_industry: bool = True) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for sec in self.securities.values():
            if use_industry and sec.industry:
                out.setdefault(f"Industry: {sec.industry}", set()).add(sec.symbol)
            for t in sec.themes:
                out.setdefault(f"Theme: {t}", set()).add(sec.symbol)
        return out

    def tick(self, symbol: str, reference_price: float, ticks: TickTable, settings: "Settings") -> Decimal:
        sec = self.get(symbol)
        if sec and sec.tick_size_raw and settings.ticks.prefer_instrument_master:
            return ticks.normalise_instrument_tick(sec.tick_size_raw, reference_price, settings.upstox.instrument_tick_unit)
        return ticks.tick_for(reference_price)

    def circuit_limits(
        self, symbol: str, prev_close: float, tick: Decimal, settings: "Settings", quote: "Quote | None" = None
    ) -> CircuitLimits:
        """Today's circuit prices: the broker quote when available, else prev close ± band."""
        if quote is not None and quote.upper_circuit and quote.lower_circuit:
            sec = self.get(symbol)
            return CircuitLimits(to_decimal(quote.lower_circuit), to_decimal(quote.upper_circuit), sec.band_pct if sec else None, "quote")
        sec = self.get(symbol)
        if sec is None or (sec.band_pct is None and not sec.is_fno):
            band, source = settings.price_bands.default_band_pct, "default-band"
        elif sec.band_pct is None:
            band, source = settings.price_bands.fno_band_pct, "fno-estimate"
        else:
            band, source = sec.band_pct, "band"
        pc = to_decimal(prev_close)
        b = to_decimal(band) / Decimal(100)
        upper = round_to_tick(pc * (1 + b), tick, "down")
        lower = round_to_tick(pc * (1 - b), tick, "up")
        return CircuitLimits(lower, upper, band, source)


def load_custom_themes(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): [str(s) for s in (v or [])] for k, v in (raw.get("themes") or {}).items()}


def _slug(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")


theme_file_slug = _slug
