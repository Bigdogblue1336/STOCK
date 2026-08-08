"""Read-only data access for the dashboard. Pulls the latest run's rows out
of portfolio.db -- never writes. Kept separate from app.py so it's importable
and testable without a Streamlit runtime.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional


def connect_readonly(db_path: str) -> sqlite3.Connection:
    abs_path = os.path.abspath(db_path)
    conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def db_exists(db_path: str) -> bool:
    return os.path.exists(db_path)


def latest_asof_date(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT MAX(asof_date) AS d FROM valuations").fetchone()
    return row["d"] if row and row["d"] else None


def list_asof_dates(conn: sqlite3.Connection, limit: int = 30) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT asof_date FROM valuations ORDER BY asof_date DESC LIMIT ?", (limit,)
    ).fetchall()
    return [r["asof_date"] for r in rows]


def load_positions(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM positions").fetchall()
    return {r["id"]: dict(r) for r in rows}


def load_valuations(conn: sqlite3.Connection, asof_date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM valuations WHERE asof_date = ? ORDER BY ticker, position_id", (asof_date,)
    ).fetchall()
    return [dict(r) for r in rows]


def load_allocations(conn: sqlite3.Connection, asof_date: str) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT * FROM allocations WHERE asof_date = ? ORDER BY dimension, value DESC", (asof_date,)
    ).fetchall()
    out: dict[str, list[dict]] = {"ticker": [], "sector": []}
    for r in rows:
        out.setdefault(r["dimension"], []).append(dict(r))
    return out


def load_portfolio_greeks(conn: sqlite3.Connection, asof_date: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM portfolio_greeks WHERE asof_date = ? ORDER BY id DESC LIMIT 1", (asof_date,)
    ).fetchone()
    return dict(row) if row else None


def load_iv_environment(conn: sqlite3.Connection, asof_date: str) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM iv_environment WHERE asof_date = ?", (asof_date,)).fetchall()
    return {r["position_id"]: dict(r) for r in rows}


def load_macro_gate(conn: sqlite3.Connection, asof_date: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM macro_gate WHERE asof_date = ?", (asof_date,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["weights"] = json.loads(d.pop("weights_json"))
    d["components"] = json.loads(d.pop("components_json"))
    return d


def load_news_analysis(conn: sqlite3.Connection, asof_date: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM news_analysis WHERE asof_date = ? ORDER BY ticker", (asof_date,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["key_drivers"] = json.loads(d.pop("key_drivers_json")) if d.get("key_drivers_json") else []
        d["headlines"] = json.loads(d.pop("headlines_json")) if d.get("headlines_json") else []
        out.append(d)
    return out


def load_alerts(conn: sqlite3.Connection, asof_date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM alerts WHERE asof_date = ? ORDER BY "
        "CASE severity WHEN 'high' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, ticker",
        (asof_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def load_chain_diffs(conn: sqlite3.Connection, asof_date: str) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM chain_diffs WHERE asof_date = ?", (asof_date,)).fetchall()
    out = {}
    for r in rows:
        out[r["ticker"]] = {
            "new_expiries": json.loads(r["new_expiries_json"]),
            "new_strikes": json.loads(r["new_strikes_json"]),
        }
    return out


def load_snapshot_on_or_before(conn: sqlite3.Connection, ticker: str, asof_date: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT data, asof_date FROM snapshots WHERE ticker = ? AND asof_date <= ? "
        "ORDER BY asof_date DESC LIMIT 1",
        (ticker, asof_date),
    ).fetchone()
    if row is None:
        return None
    return {"asof_date": row["asof_date"], "data": json.loads(row["data"])}
