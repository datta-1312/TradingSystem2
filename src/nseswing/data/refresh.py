"""Download every reference file the system uses (``nseswing refresh-reference``).

Each download is independent: a failure (e.g. NSE blocking the request) is
reported and the previous copy on disk, if any, keeps being used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from nseswing.data import nse
from nseswing.data.reference import theme_file_slug

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings
    from nseswing.core.calendar import NSECalendar


@dataclass
class RefreshResult:
    item: str
    ok: bool
    detail: str


def refresh_reference(settings: "Settings", calendar: "NSECalendar | None" = None, today: date | None = None) -> list[RefreshResult]:
    from nseswing.data.upstox import UpstoxClient

    today = today or date.today()
    ref_dir = settings.path(settings.data.reference_dir)
    ref_dir.mkdir(parents=True, exist_ok=True)
    client = nse.NseClient(settings)
    results: list[RefreshResult] = []

    def attempt(item: str, fn) -> None:
        try:
            detail = fn()
            results.append(RefreshResult(item, True, detail or "ok"))
        except Exception as exc:  # noqa: BLE001 - report and continue
            results.append(RefreshResult(item, False, f"{type(exc).__name__}: {exc}"))

    attempt("universe constituents", lambda: str(client.download(settings.universe.constituents_url, settings.path(settings.universe.constituents_file))))
    for name, url in settings.screener.themes.index_themes.items():
        attempt(f"theme index {name}", lambda url=url, name=name: str(client.download(url, ref_dir / "themes" / f"{theme_file_slug(name)}.csv")))
    attempt("price bands (sec_list)", lambda: str(client.download(settings.nse.sec_list_url, settings.path(settings.price_bands.sec_list_file))))
    attempt("F&O underlyings", lambda: str(client.download(settings.nse.fo_list_url, settings.path(settings.price_bands.fo_list_file))))
    attempt("ASM list", lambda: _save_json(client.get_json(settings.nse.asm_api), settings.path(settings.surveillance.asm_file)))
    attempt("GSM list", lambda: _save_json(client.get_json(settings.nse.gsm_api), settings.path(settings.surveillance.gsm_file)))

    def corp_actions() -> str:
        start = today - timedelta(days=settings.nse.corporate_actions_lookback_days)
        end = today + timedelta(days=60)
        url = f"{settings.nse.corporate_actions_api}?index=equities&from_date={start:%d-%m-%Y}&to_date={end:%d-%m-%Y}"
        return _save_json(client.get_json(url), settings.path(settings.corporate_actions.actions_file))

    attempt("corporate actions", corp_actions)

    def holidays() -> str:
        listed = nse.parse_holidays(client.get_json(settings.nse.holidays_api))
        if calendar is None:
            return f"{len(listed)} holidays listed by NSE"
        missing = [f"{d} {n}" for d, n in listed if calendar.holiday_name(d) is None and d.weekday() < 5]
        return "calendar file matches NSE" if not missing else "NOT in config/nse_calendar.yaml: " + "; ".join(missing)

    attempt("holiday check", holidays)
    attempt(
        "Upstox instrument master",
        lambda: str(UpstoxClient(settings).download_instruments(ref_dir / "upstox_instruments_nse.json")),
    )
    return results


def _save_json(payload, path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return str(path)
