"""Corporate-action adjustment of price and volume history.

Every return, EMA, AVWAP and swing level is computed on adjusted data. Bars
*before* an ex-date are multiplied by the action's price factor ``k`` (and their
volume by ``1/k`` for share-count changes):

* split / consolidation, face value A → B: ``k = B / A``
* bonus a:b (a new shares for every b held): ``k = b / (a + b)``
* rights a:b at price R, cum price P: ``k = TERP / P`` with ``TERP = (b·P + a·R) / (a + b)``
* dividend D, cum price P: ``k = (P − D) / P`` (only if enabled, see settings)
* demerger and anything else: a factor you supply in ``config/corporate_actions_manual.yaml``

**No double adjustment.** Some providers (Upstox, per its staff) already return
adjusted candles. In ``auto`` mode a large action is applied only when the raw
series still shows the jump at the ex-date: the observed log-move from the prior
close to the ex-date open must be closer to ``log k`` than to zero. Data that is
already adjusted shows no jump, so the action is skipped and logged.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd
import yaml

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.config import CorporateActionsCfg

Kind = Literal["split", "consolidation", "bonus", "rights", "dividend", "demerger", "manual"]


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    ex_date: date
    kind: Kind
    subject: str = ""
    old_fv: float | None = None  # split / consolidation
    new_fv: float | None = None
    ratio_new: float | None = None  # bonus / rights: new shares ...
    ratio_held: float | None = None  # ... for every N held
    amount: float | None = None  # dividend per share, or rights issue price
    price_factor: float | None = None  # manual actions only
    volume_factor: float | None = None

    def factor(self, cum_price: float | None) -> float | None:
        """Price factor for bars before the ex-date (None if it cannot be computed)."""
        if self.kind in ("split", "consolidation") and self.old_fv and self.new_fv:
            return self.new_fv / self.old_fv
        if self.kind == "bonus" and self.ratio_new and self.ratio_held:
            return self.ratio_held / (self.ratio_new + self.ratio_held)
        if self.kind == "rights" and self.ratio_new and self.ratio_held and self.amount is not None and cum_price:
            if self.amount >= cum_price:  # out-of-the-money rights do not dilute the price
                return 1.0
            terp = (self.ratio_held * cum_price + self.ratio_new * self.amount) / (self.ratio_new + self.ratio_held)
            return terp / cum_price
        if self.kind == "dividend" and self.amount and cum_price:
            return (cum_price - self.amount) / cum_price if self.amount < cum_price else None
        if self.kind in ("manual", "demerger") and self.price_factor:
            return self.price_factor
        return None

    @property
    def changes_share_count(self) -> bool:
        return self.kind in ("split", "consolidation", "bonus")


@dataclass
class AppliedAction:
    action: CorporateAction
    price_factor: float
    volume_factor: float
    decision: Literal["applied", "skipped_already_adjusted", "skipped_disabled", "skipped_no_factor", "skipped_out_of_range"]
    note: str = ""


@dataclass
class AdjustmentResult:
    df: pd.DataFrame
    actions: list[AppliedAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def applied(self) -> list[AppliedAction]:
        return [a for a in self.actions if a.decision == "applied"]


# ------------------------------------------------------------------ parsing NSE text
_NUM = r"(\d+(?:\.\d+)?)"
_RS = r"R[se]\.?\s*"
_SPLIT = re.compile(rf"(?:split|sub-?division|consolidation).*?from\s*{_RS}{_NUM}.*?to\s*{_RS}{_NUM}", re.I)
_BONUS = re.compile(rf"bonus\s*{_NUM}\s*:\s*{_NUM}", re.I)
_RIGHTS = re.compile(rf"rights\s*{_NUM}\s*:\s*{_NUM}\s*@\s*(premium\s*)?{_RS}{_NUM}", re.I)
_DIVIDEND_RS = re.compile(rf"dividend\s*-?\s*{_RS}{_NUM}", re.I)
_DIVIDEND_PCT = re.compile(rf"dividend\s*-?\s*{_NUM}\s*%", re.I)


def parse_nse_subject(symbol: str, ex_date: date, subject: str, face_value: float | None = None) -> list[CorporateAction]:
    """Turn one NSE corporate-action 'subject' line into structured actions.

    Examples::

        Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share
        Bonus 1:1
        Rights 1:5 @ Premium Rs 100/-
        Interim Dividend - Rs 5 Per Share / Special Dividend - Rs 2.50 Per Share
        Demerger
    """
    text = " ".join(subject.split())
    out: list[CorporateAction] = []
    m = _SPLIT.search(text)
    if m:
        old_fv, new_fv = float(m.group(1)), float(m.group(2))
        kind: Kind = "consolidation" if new_fv > old_fv else "split"
        out.append(CorporateAction(symbol, ex_date, kind, text, old_fv=old_fv, new_fv=new_fv))
    m = _BONUS.search(text)
    if m:
        out.append(CorporateAction(symbol, ex_date, "bonus", text, ratio_new=float(m.group(1)), ratio_held=float(m.group(2))))
    m = _RIGHTS.search(text)
    if m:
        premium = bool(m.group(3))
        price = float(m.group(4)) + ((face_value or 0.0) if premium else 0.0)
        out.append(
            CorporateAction(symbol, ex_date, "rights", text, ratio_new=float(m.group(1)), ratio_held=float(m.group(2)), amount=price)
        )
    dividends = [float(x) for x in _DIVIDEND_RS.findall(text)]
    if not dividends and face_value:
        dividends = [face_value * float(x) / 100.0 for x in _DIVIDEND_PCT.findall(text)]
    if dividends:
        out.append(CorporateAction(symbol, ex_date, "dividend", text, amount=round(sum(dividends), 4)))
    if re.search(r"demerger", text, re.I):
        out.append(CorporateAction(symbol, ex_date, "demerger", text))
    return out


def parse_nse_actions_json(payload: list[dict] | dict) -> list[CorporateAction]:
    """Parse the NSE corporate-actions API payload (a list of records)."""
    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    out: list[CorporateAction] = []
    for row in rows:
        ex = row.get("exDate") or row.get("ex_date")
        if not ex or str(ex).strip() in {"-", ""}:
            continue
        ex_date = _parse_nse_date(str(ex))
        fv = row.get("faceVal") or row.get("face_value")
        try:
            fv_f = float(fv) if fv not in (None, "", "-") else None
        except ValueError:
            fv_f = None
        out.extend(parse_nse_subject(str(row.get("symbol", "")).strip(), ex_date, str(row.get("subject", "")), fv_f))
    return out


def load_actions(actions_file: Path, manual_file: Path) -> list[CorporateAction]:
    actions: list[CorporateAction] = []
    if actions_file.exists():
        actions.extend(parse_nse_actions_json(json.loads(actions_file.read_text(encoding="utf-8"))))
    if manual_file.exists():
        raw = yaml.safe_load(manual_file.read_text(encoding="utf-8")) or {}
        for item in raw.get("actions", []) or []:
            ex = item["ex_date"]
            actions.append(
                CorporateAction(
                    symbol=str(item["symbol"]),
                    ex_date=ex if isinstance(ex, date) else date.fromisoformat(str(ex)),
                    kind="manual",
                    subject=str(item.get("note", item.get("kind", "manual"))),
                    price_factor=float(item["price_factor"]),
                    volume_factor=float(item.get("volume_factor", 1.0)),
                )
            )
    return actions


def _parse_nse_date(text: str) -> date:
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised NSE date {text!r}")


# ------------------------------------------------------------------ adjustment
def adjust_ohlcv(df: pd.DataFrame, actions: list[CorporateAction], cfg: "CorporateActionsCfg") -> AdjustmentResult:
    """Return an adjusted copy of a daily OHLCV frame plus a log of every decision."""
    out = df.copy().astype({c: float for c in ("open", "high", "low", "close", "volume") if c in df.columns})
    result = AdjustmentResult(out)
    if df.empty:
        return result
    if cfg.mode == "never":
        result.actions = [AppliedAction(a, 1.0, 1.0, "skipped_disabled", "corporate_actions.mode = never") for a in actions]
        result.warnings.extend(unexplained_gaps(out, actions, cfg.unexplained_gap_warn_pct))
        return result
    dates = pd.DatetimeIndex(df.index).normalize()
    price_mult = pd.Series(1.0, index=df.index)
    vol_mult = pd.Series(1.0, index=df.index)
    raw_open, raw_close = df["open"].astype(float), df["close"].astype(float)
    verify_log = abs(math.log(1.0 - cfg.verify_min_jump_pct / 100.0))

    for act in sorted(actions, key=lambda a: a.ex_date):
        ex_ts = pd.Timestamp(act.ex_date)
        ex_positions = (dates >= ex_ts).nonzero()[0]
        if len(ex_positions) == 0 or ex_positions[0] == 0:
            result.actions.append(AppliedAction(act, 1.0, 1.0, "skipped_out_of_range", "ex-date outside the data"))
            continue
        ex_pos = int(ex_positions[0])
        cum_price = float(raw_close.iloc[ex_pos - 1])
        k = act.factor(cum_price)
        if k is None or k <= 0:
            note = "needs a manual factor (config/corporate_actions_manual.yaml)" if act.kind == "demerger" else "no factor"
            result.actions.append(AppliedAction(act, 1.0, 1.0, "skipped_no_factor", note))
            if act.kind == "demerger":
                result.warnings.append(f"{act.symbol}: demerger on {act.ex_date} needs a manual price factor")
            continue
        if not _enabled(act, k, cfg):
            result.actions.append(AppliedAction(act, k, 1.0, "skipped_disabled", "disabled in settings"))
            continue
        if act.kind in ("manual", "demerger"):
            vk = act.volume_factor or 1.0
        else:
            vk = (1.0 / k) if (cfg.adjust_volume and act.changes_share_count) else 1.0
        if cfg.mode == "auto" and act.kind != "manual" and abs(math.log(k)) >= verify_log:
            observed = math.log(float(raw_open.iloc[ex_pos]) / cum_price)
            expected = math.log(k)
            if abs(observed - expected) >= abs(observed):
                result.actions.append(
                    AppliedAction(act, k, vk, "skipped_already_adjusted", f"no jump at ex-date (observed {math.exp(observed):.3f}× vs expected {k:.3f}×)")
                )
                continue
        mask = pd.Series(dates < ex_ts, index=df.index)
        price_mult[mask] *= k
        vol_mult[mask] *= vk
        result.actions.append(AppliedAction(act, k, vk, "applied"))

    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * price_mult
    out["volume"] = out["volume"] * vol_mult
    result.warnings.extend(unexplained_gaps(out, actions, cfg.unexplained_gap_warn_pct))
    return result


def _enabled(act: CorporateAction, k: float, cfg: "CorporateActionsCfg") -> bool:
    if act.kind == "rights":
        return cfg.adjust_rights
    if act.kind == "dividend":
        if cfg.adjust_dividends == "none":
            return False
        if cfg.adjust_dividends == "large_only":
            return (1.0 - k) * 100.0 >= cfg.large_dividend_min_pct
    return True


def unexplained_gaps(df: pd.DataFrame, actions: list[CorporateAction], warn_pct: float) -> list[str]:
    """Overnight moves big enough to be an unadjusted split/bonus (or a double adjustment)."""
    if len(df) < 2:
        return []
    threshold = abs(math.log(1.0 - warn_pct / 100.0))
    ex_dates = {a.ex_date for a in actions}
    prev_close = df["close"].shift(1)
    jumps = (df["open"] / prev_close).dropna()
    warnings = []
    for ts, ratio in jumps.items():
        if ratio <= 0:
            continue
        if abs(math.log(ratio)) >= threshold and pd.Timestamp(ts).date() not in ex_dates:
            warnings.append(
                f"{pd.Timestamp(ts).date()}: open/prev-close = {ratio:.3f}× with no recorded corporate action "
                "(possible unadjusted split/bonus, or a double adjustment)"
            )
    return warnings


def adjust_intraday(df: pd.DataFrame, adjustment: AdjustmentResult) -> pd.DataFrame:
    """Apply the factors chosen for the daily series to intraday bars (by session date)."""
    out = df.copy().astype({c: float for c in ("open", "high", "low", "close", "volume") if c in df.columns})
    if df.empty:
        return out
    session_dates = pd.DatetimeIndex(df.index).tz_localize(None).normalize() if df.index.tz is not None else pd.DatetimeIndex(df.index).normalize()
    for a in adjustment.applied:
        mask = session_dates < pd.Timestamp(a.action.ex_date)
        for col in ("open", "high", "low", "close"):
            out.loc[mask, col] = out.loc[mask, col] * a.price_factor
        out.loc[mask, "volume"] = out.loc[mask, "volume"] * a.volume_factor
    return out


__all__ = [
    "CorporateAction",
    "AppliedAction",
    "AdjustmentResult",
    "parse_nse_subject",
    "parse_nse_actions_json",
    "load_actions",
    "adjust_ohlcv",
    "adjust_intraday",
    "unexplained_gaps",
]
