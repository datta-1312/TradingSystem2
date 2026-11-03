"""Trade journal (SQLite): entries, exits, charges and the R-multiple result.

Money is stored as TEXT decimals so nothing is lost to floating point. For a
closed trade:

    gross P&L  = Σ sells − Σ buys
    charges    = Σ charges of every fill
    net P&L    = gross − charges
    R (gross)  = gross P&L / (shares × initial risk per share)
    R (net)    = net P&L   / (shares × initial risk per share)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:  # pragma: no cover
    from nseswing.costs import LegCharges
    from nseswing.risk import TradePlan

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, mode TEXT NOT NULL, theme TEXT, zone TEXT,
    entry_time TEXT, entry_price TEXT, initial_stop TEXT, r_per_share TEXT, shares INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    exit_time TEXT, avg_exit TEXT, gross_pnl TEXT, charges TEXT, net_pnl TEXT, r_gross TEXT, r_net TEXT,
    plan_json TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL REFERENCES trades(id),
    ts TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, price TEXT NOT NULL,
    purpose TEXT, charges_total TEXT, charges_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER, ts TEXT NOT NULL, kind TEXT NOT NULL, symbol TEXT, message TEXT, payload_json TEXT
);
"""

TWO = Decimal("0.01")


@dataclass
class TradeResult:
    trade_id: int
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    r_gross: Decimal
    r_net: Decimal
    avg_exit: Decimal


class Journal:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ writes
    def open_trade(self, plan: "TradePlan", mode: str, ts: datetime, entry_price: Decimal, shares: int, stop: Decimal, notes: str = "") -> int:
        plan_json = json.dumps(
            {
                "entry": str(plan.entry),
                "stop": str(plan.stop),
                "shares": plan.shares,
                "risk_per_share": str(plan.risk_per_share),
                "targets": {k: str(v) for k, v in plan.targets.items()},
                "net_targets": {k: str(v) for k, v in plan.net_targets.items()},
                "stop_method": plan.stop_method,
                "position_pct": str(plan.position_pct),
                "warnings": plan.warnings,
                "flags": plan.flags,
            }
        )
        cur = self.conn.execute(
            "INSERT INTO trades(symbol, mode, theme, zone, entry_time, entry_price, initial_stop, r_per_share, shares, plan_json, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (plan.symbol, mode, plan.theme, plan.zone.describe() if plan.zone else "", ts.isoformat(), str(entry_price), str(stop), str(entry_price - stop), shares, plan_json, notes),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_fill(self, trade_id: int, ts: datetime, side: str, qty: int, price: Decimal, purpose: str, charges: "LegCharges") -> None:
        self.conn.execute(
            "INSERT INTO fills(trade_id, ts, side, qty, price, purpose, charges_total, charges_json) VALUES (?,?,?,?,?,?,?,?)",
            (trade_id, ts.isoformat(), side, qty, str(price), purpose, str(charges.total), json.dumps(charges.as_dict())),
        )
        self.conn.commit()

    def log_event(self, ts: datetime, kind: str, symbol: str, message: str, trade_id: int | None = None, payload: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(trade_id, ts, kind, symbol, message, payload_json) VALUES (?,?,?,?,?,?)",
            (trade_id, ts.isoformat(), kind, symbol, message, json.dumps(payload or {}, default=str)),
        )
        self.conn.commit()

    def close_trade(self, trade_id: int, ts: datetime) -> TradeResult:
        res = self.result(trade_id)
        self.conn.execute(
            "UPDATE trades SET status='closed', exit_time=?, avg_exit=?, gross_pnl=?, charges=?, net_pnl=?, r_gross=?, r_net=? WHERE id=?",
            (ts.isoformat(), str(res.avg_exit), str(res.gross_pnl), str(res.charges), str(res.net_pnl), str(res.r_gross), str(res.r_net), trade_id),
        )
        self.conn.commit()
        return res

    # ------------------------------------------------------------------ reads
    def result(self, trade_id: int) -> TradeResult:
        t = self.conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        fills = self.conn.execute("SELECT * FROM fills WHERE trade_id=?", (trade_id,)).fetchall()
        buys = sum((Decimal(f["price"]) * f["qty"] for f in fills if f["side"] == "BUY"), Decimal(0))
        sells = sum((Decimal(f["price"]) * f["qty"] for f in fills if f["side"] == "SELL"), Decimal(0))
        sold = sum((f["qty"] for f in fills if f["side"] == "SELL"), 0)
        charges = sum((Decimal(f["charges_total"]) for f in fills), Decimal(0))
        gross = sells - buys
        net = gross - charges
        risk = Decimal(t["r_per_share"]) * int(t["shares"])
        r_gross = (gross / risk).quantize(TWO, ROUND_HALF_UP) if risk else Decimal(0)
        r_net = (net / risk).quantize(TWO, ROUND_HALF_UP) if risk else Decimal(0)
        avg_exit = (sells / sold).quantize(TWO, ROUND_HALF_UP) if sold else Decimal(0)
        return TradeResult(trade_id, gross.quantize(TWO), charges.quantize(TWO), net.quantize(TWO), r_gross, r_net, avg_exit)

    def trades(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT id, symbol, mode, theme, entry_time, entry_price, initial_stop, r_per_share, shares, status, exit_time, avg_exit, gross_pnl, charges, net_pnl, r_gross, r_net, zone FROM trades ORDER BY id", self.conn)

    def fills(self, trade_id: int | None = None) -> pd.DataFrame:
        q = "SELECT * FROM fills" + (" WHERE trade_id=?" if trade_id is not None else "") + " ORDER BY id"
        return pd.read_sql_query(q, self.conn, params=(trade_id,) if trade_id is not None else ())

    def events(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM events ORDER BY id", self.conn)

    def export_csv(self, directory: Path) -> list[Path]:
        directory.mkdir(parents=True, exist_ok=True)
        out = []
        for name, df in (("trades", self.trades()), ("fills", self.fills()), ("events", self.events())):
            p = directory / f"journal_{name}.csv"
            df.to_csv(p, index=False)
            out.append(p)
        return out
