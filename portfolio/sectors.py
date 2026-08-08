"""Ticker -> sector resolution: config lookup first, yfinance fallback second.

Fallback lookups are cached in the `sector_cache` SQLite table so repeat runs
don't re-hit yfinance for tickers already resolved.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

UNKNOWN_SECTOR = "Unknown"


def load_sector_map(path: str) -> dict[str, str]:
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {k.upper(): v for k, v in raw.items()}


def _cached_sector(conn: sqlite3.Connection, ticker: str) -> Optional[str]:
    row = conn.execute("SELECT sector FROM sector_cache WHERE ticker = ?", (ticker,)).fetchone()
    return row["sector"] if row else None


def _store_cache(conn: sqlite3.Connection, ticker: str, sector: str, source: str) -> None:
    conn.execute(
        """
        INSERT INTO sector_cache (ticker, sector, source, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            sector=excluded.sector, source=excluded.source, updated_at=excluded.updated_at
        """,
        (ticker, sector, source, datetime.utcnow().isoformat() + "Z"),
    )


def get_sector(conn: sqlite3.Connection, ticker: str, sector_map: dict[str, str]) -> str:
    """Resolve a ticker's sector: config map -> DB cache -> yfinance -> "Unknown"."""
    ticker = ticker.upper()

    mapped = sector_map.get(ticker)
    if mapped:
        return mapped

    cached = _cached_sector(conn, ticker)
    if cached:
        return cached

    from . import market_data

    sector = None
    try:
        sector = market_data.get_sector_fallback(ticker)
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning("sector fallback failed for %s: %s", ticker, exc)

    resolved = sector or UNKNOWN_SECTOR
    _store_cache(conn, ticker, resolved, source="yfinance" if sector else "unknown")
    return resolved
