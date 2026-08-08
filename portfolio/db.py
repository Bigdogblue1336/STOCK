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

CREATE TABLE IF NOT EXISTS sector_cache (
    ticker      TEXT PRIMARY KEY,
    sector      TEXT NOT NULL,
    source      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date       TEXT NOT NULL,
    run_timestamp   TEXT NOT NULL,
    dimension       TEXT NOT NULL,  -- 'ticker' | 'sector'
    key             TEXT NOT NULL,
    value           REAL NOT NULL,
    pct_of_total    REAL NOT NULL,
    cap_pct         REAL NOT NULL,
    over_cap        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS portfolio_greeks (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date                   TEXT NOT NULL,
    run_timestamp               TEXT NOT NULL,
    total_value                 REAL,
    net_delta_shares            REAL,
    daily_theta_dollars         REAL,
    net_vega_dollars_per_point  REAL
);

CREATE TABLE IF NOT EXISTS iv_environment (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date       TEXT NOT NULL,
    run_timestamp   TEXT NOT NULL,
    position_id     TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    current_iv      REAL,
    iv_rank         REAL,
    iv_percentile   REAL,
    sample_size     INTEGER NOT NULL,
    status          TEXT NOT NULL,  -- 'ok' | 'building_history'
    rich_cheap      TEXT,           -- 'rich' | 'cheap' | 'normal' | NULL
    dte             INTEGER,
    near_expiry     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS macro_gate (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date       TEXT NOT NULL,
    run_timestamp   TEXT NOT NULL,
    composite_score REAL,
    weights_json    TEXT NOT NULL,
    components_json TEXT NOT NULL,
    UNIQUE(asof_date)
);

CREATE TABLE IF NOT EXISTS news_analysis (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                  TEXT NOT NULL,
    asof_date               TEXT NOT NULL,
    run_timestamp           TEXT NOT NULL,
    summary                 TEXT,
    sentiment               TEXT,  -- 'positive' | 'neutral' | 'negative'
    key_drivers_json        TEXT,
    position_flag           INTEGER NOT NULL DEFAULT 0,
    position_flag_detail    TEXT,
    headlines_json          TEXT,
    model                   TEXT,
    UNIQUE(ticker, asof_date)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_date ON snapshots(ticker, asof_date);
CREATE INDEX IF NOT EXISTS idx_valuations_position_date ON valuations(position_id, asof_date);
CREATE INDEX IF NOT EXISTS idx_allocations_asof ON allocations(asof_date, dimension);
CREATE INDEX IF NOT EXISTS idx_iv_environment_position_date ON iv_environment(position_id, asof_date);
CREATE INDEX IF NOT EXISTS idx_news_analysis_ticker_date ON news_analysis(ticker, asof_date);
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


def get_snapshots_in_range(
    conn: sqlite3.Connection, ticker: str, start_date: str, end_date: str
) -> list[dict]:
    """All stored snapshots for `ticker` with start_date <= asof_date <= end_date, ascending."""
    rows = conn.execute(
        """
        SELECT asof_date, data FROM snapshots
        WHERE ticker = ? AND asof_date >= ? AND asof_date <= ?
        ORDER BY asof_date ASC
        """,
        (ticker, start_date, end_date),
    ).fetchall()
    return [{"asof_date": r["asof_date"], "data": json.loads(r["data"])} for r in rows]


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


def insert_allocation_row(
    conn: sqlite3.Connection,
    asof_date: str,
    run_timestamp: str,
    dimension: str,
    key: str,
    value: float,
    pct_of_total: float,
    cap_pct: float,
    over_cap: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO allocations
            (asof_date, run_timestamp, dimension, key, value, pct_of_total, cap_pct, over_cap)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (asof_date, run_timestamp, dimension, key, value, pct_of_total, cap_pct, int(over_cap)),
    )


def insert_portfolio_greeks(
    conn: sqlite3.Connection,
    asof_date: str,
    run_timestamp: str,
    total_value: float,
    net_delta_shares: float,
    daily_theta_dollars: float,
    net_vega_dollars_per_point: float,
) -> None:
    conn.execute(
        """
        INSERT INTO portfolio_greeks
            (asof_date, run_timestamp, total_value, net_delta_shares,
             daily_theta_dollars, net_vega_dollars_per_point)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (asof_date, run_timestamp, total_value, net_delta_shares,
         daily_theta_dollars, net_vega_dollars_per_point),
    )


def insert_iv_environment(conn: sqlite3.Connection, asof_date: str, run_timestamp: str, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO iv_environment
            (asof_date, run_timestamp, position_id, ticker, current_iv, iv_rank,
             iv_percentile, sample_size, status, rich_cheap, dte, near_expiry)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asof_date, run_timestamp, row["position_id"], row["ticker"], row["current_iv"],
            row["iv_rank"], row["iv_percentile"], row["sample_size"], row["status"],
            row["rich_cheap"], row["dte"], int(row["near_expiry"]),
        ),
    )


def insert_macro_gate(
    conn: sqlite3.Connection,
    asof_date: str,
    run_timestamp: str,
    composite_score: Optional[float],
    weights: dict[str, float],
    components: list[dict],
) -> None:
    conn.execute(
        """
        INSERT INTO macro_gate (asof_date, run_timestamp, composite_score, weights_json, components_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(asof_date) DO UPDATE SET
            run_timestamp=excluded.run_timestamp,
            composite_score=excluded.composite_score,
            weights_json=excluded.weights_json,
            components_json=excluded.components_json
        """,
        (asof_date, run_timestamp, composite_score, json.dumps(weights), json.dumps(components)),
    )


def get_news_analysis(conn: sqlite3.Connection, ticker: str, asof_date: str) -> Optional[dict]:
    """Cache lookup: today's stored analysis for `ticker`, or None if we haven't called Claude yet."""
    row = conn.execute(
        "SELECT * FROM news_analysis WHERE ticker = ? AND asof_date = ?",
        (ticker, asof_date),
    ).fetchone()
    if row is None:
        return None
    return {
        "ticker": row["ticker"],
        "asof_date": row["asof_date"],
        "summary": row["summary"],
        "sentiment": row["sentiment"],
        "key_drivers": json.loads(row["key_drivers_json"]) if row["key_drivers_json"] else [],
        "position_flag": bool(row["position_flag"]),
        "position_flag_detail": row["position_flag_detail"] or "",
        "headlines": json.loads(row["headlines_json"]) if row["headlines_json"] else [],
        "model": row["model"],
    }


def save_news_analysis(conn: sqlite3.Connection, run_timestamp: str, analysis: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO news_analysis
            (ticker, asof_date, run_timestamp, summary, sentiment, key_drivers_json,
             position_flag, position_flag_detail, headlines_json, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date) DO NOTHING
        """,
        (
            analysis["ticker"],
            analysis["asof_date"],
            run_timestamp,
            analysis["summary"],
            analysis["sentiment"],
            json.dumps(analysis["key_drivers"]),
            int(analysis["position_flag"]),
            analysis["position_flag_detail"],
            json.dumps(analysis["headlines"]),
            analysis.get("model"),
        ),
    )
