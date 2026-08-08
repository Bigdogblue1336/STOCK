"""SQLite persistence: positions, dated chain snapshots, and valuation history.

Everything lives in one database file so later layers (alerts, reporting,
IV-history analysis, new-strike/new-expiry diffing) can query it directly.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id              TEXT PRIMARY KEY,
    asset_type      TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    option_type     TEXT,
    strike          REAL,
    expiry          TEXT,
    entry_price     REAL NOT NULL,
    contracts       REAL NOT NULL,
    entry_date      TEXT NOT NULL,
    target_price    REAL,
    stop_price      REAL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    asof_date       TEXT NOT NULL,
    pulled_at       TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    data            TEXT NOT NULL,
    UNIQUE(ticker, asof_date)
);

CREATE TABLE IF NOT EXISTS valuations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id             TEXT NOT NULL,
    asof_date               TEXT NOT NULL,
    run_timestamp           TEXT NOT NULL,
    ticker                  TEXT NOT NULL,
    asset_type              TEXT NOT NULL,
    bid                     REAL,
    ask                     REAL,
    last                    REAL,
    mark                    REAL,
    iv                      REAL,
    volume                  INTEGER,
    open_interest           INTEGER,
    current_value           REAL,
    unrealized_pnl          REAL,
    unrealized_pnl_pct      REAL,
    dte                     INTEGER,
    progress_to_target_pct  REAL,
    progress_to_stop_pct    REAL,
    delta                   REAL,
    gamma                   REAL,
    theta                   REAL,
    vega                    REAL,
    FOREIGN KEY (position_id) REFERENCES positions(id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_date ON snapshots(ticker, asof_date);
CREATE INDEX IF NOT EXISTS idx_valuations_position_date ON valuations(position_id, asof_date);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def open_db(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_position(conn: sqlite3.Connection, position, updated_at: str) -> None:
    conn.execute(
        """
        INSERT INTO positions (
            id, asset_type, ticker, option_type, strike, expiry,
            entry_price, contracts, entry_date, target_price, stop_price, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            asset_type=excluded.asset_type,
            ticker=excluded.ticker,
            option_type=excluded.option_type,
            strike=excluded.strike,
            expiry=excluded.expiry,
            entry_price=excluded.entry_price,
            contracts=excluded.contracts,
            entry_date=excluded.entry_date,
            target_price=excluded.target_price,
            stop_price=excluded.stop_price,
            updated_at=excluded.updated_at
        """,
        (
            position.id,
            position.asset_type,
            position.ticker,
            position.option_type,
            position.strike,
            position.expiry,
            position.entry_price,
            position.contracts,
            position.entry_date,
            position.target_price,
            position.stop_price,
            updated_at,
        ),
    )


def save_snapshot(
    conn: sqlite3.Connection,
    ticker: str,
    asof_date: str,
    pulled_at: str,
    file_path: str,
    data: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO snapshots (ticker, asof_date, pulled_at, file_path, data)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date) DO UPDATE SET
            pulled_at=excluded.pulled_at,
            file_path=excluded.file_path,
            data=excluded.data
        """,
        (ticker, asof_date, pulled_at, file_path, json.dumps(data)),
    )


def get_prior_snapshot(
    conn: sqlite3.Connection, ticker: str, before_date: str
) -> Optional[dict]:
    """Most recent snapshot for `ticker` strictly before `before_date` (for diffing)."""
    row = conn.execute(
        """
        SELECT data, asof_date FROM snapshots
        WHERE ticker = ? AND asof_date < ?
        ORDER BY asof_date DESC LIMIT 1
        """,
        (ticker, before_date),
    ).fetchone()
    if row is None:
        return None
    return {"asof_date": row["asof_date"], "data": json.loads(row["data"])}


def insert_valuation(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    columns = [
        "position_id", "asof_date", "run_timestamp", "ticker", "asset_type",
        "bid", "ask", "last", "mark", "iv", "volume", "open_interest",
        "current_value", "unrealized_pnl", "unrealized_pnl_pct", "dte",
        "progress_to_target_pct", "progress_to_stop_pct",
        "delta", "gamma", "theta", "vega",
    ]
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO valuations ({', '.join(columns)}) VALUES ({placeholders})",
        [row.get(c) for c in columns],
    )


def latest_valuations(conn: sqlite3.Connection, asof_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM valuations WHERE asof_date = ? ORDER BY ticker, position_id",
        (asof_date,),
    ).fetchall()
