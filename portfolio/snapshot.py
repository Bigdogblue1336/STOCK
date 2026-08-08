"""Persist a dated snapshot of each underlying's pulled option chain.

Every run writes snapshots/TICKER_YYYY-MM-DD.json and mirrors the same
payload into the `snapshots` SQLite table (keyed on ticker + asof_date, so
re-running on the same day overwrites that day's snapshot rather than
duplicating it). Later layers diff today's snapshot against the prior one
via db.get_prior_snapshot() to detect new strikes/expiries and build IV
history.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime


def snapshot_path(snapshot_dir: str, ticker: str, asof: date) -> str:
    return os.path.join(snapshot_dir, f"{ticker}_{asof.isoformat()}.json")


def write_snapshot(
    conn: sqlite3.Connection,
    snapshot_dir: str,
    ticker: str,
    asof: date,
    chain_data: dict,
) -> str:
    os.makedirs(snapshot_dir, exist_ok=True)
    path = snapshot_path(snapshot_dir, ticker, asof)

    with open(path, "w") as f:
        json.dump(chain_data, f, indent=2, sort_keys=True)

    from . import db as db_module

    db_module.save_snapshot(
        conn,
        ticker=ticker,
        asof_date=asof.isoformat(),
        pulled_at=chain_data.get("pulled_at", datetime.utcnow().isoformat() + "Z"),
        file_path=path,
        data=chain_data,
    )
    return path
