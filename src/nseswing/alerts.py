"""Alert bus: every rule that fires produces an :class:`Alert`.

Sinks: console, a JSONL file (``alerts.file``) and optionally Telegram. The
replay mode and the dashboard read the same alert list, so you see exactly
when each rule fired, in IST.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

import requests

from nseswing.core.timeutil import fmt_ist, now_ist, to_ist

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import Settings

Level = Literal["debug", "info", "warning", "critical"]
_RANK = {"debug": 0, "info": 1, "warning": 2, "critical": 3}


@dataclass
class Alert:
    ts: datetime
    level: Level
    kind: str  # e.g. PREOPEN_WATCH, ZONE_APPROACH, TRIGGER, ENTRY_FILLED, STOP_PLACED, TARGET_PARTIAL ...
    symbol: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        return f"{fmt_ist(self.ts)}  {self.level.upper():<8} {self.kind:<22} {self.symbol:<12} {self.message}"


class AlertBus:
    def __init__(self, settings: "Settings | None" = None, console: bool | None = None, file: Path | None = None, sinks: list[Callable[[Alert], None]] | None = None):
        self.alerts: list[Alert] = []
        self.min_level: Level = settings.alerts.min_level if settings else "info"
        self.console = settings.alerts.console if (settings and console is None) else bool(console)
        self.file = file if file is not None else (settings.path(settings.alerts.file) if settings else None)
        self.sinks = list(sinks or [])
        self._telegram = None
        if settings and settings.alerts.telegram.enabled:
            token = os.environ.get(settings.alerts.telegram.bot_token_env)
            chat = os.environ.get(settings.alerts.telegram.chat_id_env)
            if token and chat:
                self._telegram = (token, chat)

    def emit(self, kind: str, symbol: str, message: str, level: Level = "info", ts: datetime | None = None, **data: Any) -> Alert:
        alert = Alert(to_ist(ts).to_pydatetime() if ts is not None else now_ist(), level, kind, symbol, message, data)
        self.alerts.append(alert)
        if _RANK[level] < _RANK[self.min_level]:
            return alert
        if self.console:
            print(alert.line())
        if self.file is not None:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.file.open("a", encoding="utf-8") as fh:
                rec = asdict(alert)
                rec["ts"] = alert.ts.isoformat()
                fh.write(json.dumps(rec, default=str) + "\n")
        for sink in self.sinks:
            sink(alert)
        if self._telegram and _RANK[level] >= _RANK["info"]:
            self._send_telegram(alert)
        return alert

    def _send_telegram(self, alert: Alert) -> None:  # pragma: no cover - network
        token, chat = self._telegram
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": alert.line()}, timeout=10)
        except requests.RequestException:
            pass

    def of_kind(self, kind: str) -> list[Alert]:
        return [a for a in self.alerts if a.kind == kind]

    def kinds(self) -> list[str]:
        return [a.kind for a in self.alerts]
