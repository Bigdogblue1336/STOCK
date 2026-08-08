"""Allocation by ticker and by sector, with concentration flags.

Concentration caps are informational only: exceeding one produces a flag in
the report/DB, never blocks a run or a position.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import sectors as sectors_module
from .config import DEFAULT_SECTOR_CONCENTRATION_CAP, DEFAULT_TICKER_CONCENTRATION_CAP
from .models import Position
from .valuation import Valuation


@dataclass
class AllocationRow:
    key: str
    value: float
    pct_of_total: float
    over_cap: bool


@dataclass
class AllocationResult:
    total_value: float
    by_ticker: list[AllocationRow]
    by_sector: list[AllocationRow]
    ticker_cap: float
    sector_cap: float
    flags: list[str]


def compute_allocation(
    conn: sqlite3.Connection,
    positions: list[Position],
    valuations: list[Valuation],
    sector_map: Optional[dict[str, str]] = None,
    ticker_cap: float = DEFAULT_TICKER_CONCENTRATION_CAP,
    sector_cap: float = DEFAULT_SECTOR_CONCENTRATION_CAP,
) -> AllocationResult:
    sector_map = sector_map or {}
    value_by_position = {v.position_id: (v.current_value or 0.0) for v in valuations}
    total_value = sum(value_by_position.values())

    by_ticker_totals: dict[str, float] = {}
    by_sector_totals: dict[str, float] = {}
    resolved_sectors: dict[str, str] = {}

    for pos in positions:
        value = value_by_position.get(pos.id, 0.0)
        by_ticker_totals[pos.ticker] = by_ticker_totals.get(pos.ticker, 0.0) + value

        sector = resolved_sectors.get(pos.ticker)
        if sector is None:
            sector = sectors_module.get_sector(conn, pos.ticker, sector_map)
            resolved_sectors[pos.ticker] = sector
        by_sector_totals[sector] = by_sector_totals.get(sector, 0.0) + value

    def build_rows(totals: dict[str, float], cap: float) -> list[AllocationRow]:
        rows = []
        for key, value in sorted(totals.items(), key=lambda kv: -kv[1]):
            pct = (value / total_value * 100.0) if total_value else 0.0
            rows.append(AllocationRow(key=key, value=value, pct_of_total=pct, over_cap=pct > cap * 100.0))
        return rows

    by_ticker = build_rows(by_ticker_totals, ticker_cap)
    by_sector = build_rows(by_sector_totals, sector_cap)

    flags = []
    for row in by_ticker:
        if row.over_cap:
            flags.append(
                f"Ticker concentration: {row.key} is {row.pct_of_total:.1f}% of portfolio "
                f"(cap {ticker_cap * 100:.0f}%)"
            )
    for row in by_sector:
        if row.over_cap:
            flags.append(
                f"Sector concentration: {row.key} is {row.pct_of_total:.1f}% of portfolio "
                f"(cap {sector_cap * 100:.0f}%)"
            )

    return AllocationResult(
        total_value=total_value,
        by_ticker=by_ticker,
        by_sector=by_sector,
        ticker_cap=ticker_cap,
        sector_cap=sector_cap,
        flags=flags,
    )
