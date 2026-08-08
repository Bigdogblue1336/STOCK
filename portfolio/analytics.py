"""Portfolio layer: ties allocation, aggregate greeks, and IV environment
together into one analytics summary per run, and persists it to SQLite.

Consumes the per-position Valuation objects already produced by Layer 1
(portfolio.valuation / portfolio.cli) -- it does not re-pull market data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import db as db_module
from .aggregate_greeks import AggregateGreeks, compute_aggregate_greeks
from .allocation import AllocationResult, compute_allocation
from .config import (
    DEFAULT_IV_CHEAP_THRESHOLD,
    DEFAULT_IV_LOOKBACK_DAYS,
    DEFAULT_IV_MIN_HISTORY_DAYS,
    DEFAULT_IV_RICH_THRESHOLD,
    DEFAULT_NEAR_EXPIRY_DTE,
    DEFAULT_SECTOR_CONCENTRATION_CAP,
    DEFAULT_TICKER_CONCENTRATION_CAP,
)
from .iv_environment import IVEnvironment, compute_iv_environment
from .models import Position
from .valuation import Valuation


@dataclass
class PortfolioAnalytics:
    asof_date: str
    allocation: AllocationResult
    greeks: AggregateGreeks
    iv_environment: list[IVEnvironment]


def compute_portfolio_analytics(
    conn: sqlite3.Connection,
    positions: list[Position],
    valuations: list[Valuation],
    asof: date,
    sector_map: Optional[dict[str, str]] = None,
    ticker_cap: float = DEFAULT_TICKER_CONCENTRATION_CAP,
    sector_cap: float = DEFAULT_SECTOR_CONCENTRATION_CAP,
    iv_lookback_days: int = DEFAULT_IV_LOOKBACK_DAYS,
    iv_min_history_days: int = DEFAULT_IV_MIN_HISTORY_DAYS,
    iv_rich_threshold: float = DEFAULT_IV_RICH_THRESHOLD,
    iv_cheap_threshold: float = DEFAULT_IV_CHEAP_THRESHOLD,
    near_expiry_dte: int = DEFAULT_NEAR_EXPIRY_DTE,
) -> PortfolioAnalytics:
    allocation = compute_allocation(
        conn, positions, valuations, sector_map=sector_map, ticker_cap=ticker_cap, sector_cap=sector_cap
    )
    greeks = compute_aggregate_greeks(positions, valuations)

    valuation_by_id = {v.position_id: v for v in valuations}
    iv_rows = []
    for pos in positions:
        if not pos.is_option:
            continue
        v = valuation_by_id.get(pos.id)
        current_iv = v.iv if v is not None else None
        iv_rows.append(
            compute_iv_environment(
                conn,
                pos,
                current_iv,
                asof,
                lookback_days=iv_lookback_days,
                min_history_days=iv_min_history_days,
                rich_threshold=iv_rich_threshold,
                cheap_threshold=iv_cheap_threshold,
                near_expiry_dte=near_expiry_dte,
            )
        )

    return PortfolioAnalytics(
        asof_date=asof.isoformat(), allocation=allocation, greeks=greeks, iv_environment=iv_rows
    )


def save_portfolio_analytics(
    conn: sqlite3.Connection, asof_date: str, run_timestamp: str, analytics: PortfolioAnalytics
) -> None:
    for row in analytics.allocation.by_ticker:
        db_module.insert_allocation_row(
            conn, asof_date, run_timestamp, "ticker", row.key, row.value,
            row.pct_of_total, analytics.allocation.ticker_cap * 100.0, row.over_cap,
        )
    for row in analytics.allocation.by_sector:
        db_module.insert_allocation_row(
            conn, asof_date, run_timestamp, "sector", row.key, row.value,
            row.pct_of_total, analytics.allocation.sector_cap * 100.0, row.over_cap,
        )

    db_module.insert_portfolio_greeks(
        conn, asof_date, run_timestamp,
        analytics.greeks.total_value, analytics.greeks.net_delta_shares,
        analytics.greeks.daily_theta_dollars, analytics.greeks.net_vega_dollars_per_point,
    )

    for iv_row in analytics.iv_environment:
        db_module.insert_iv_environment(conn, asof_date, run_timestamp, iv_row.to_dict())


def print_analytics_report(analytics: PortfolioAnalytics) -> None:
    alloc = analytics.allocation
    print(f"\nAllocation (total value: {alloc.total_value:,.2f})")
    print("-" * 72)
    print(f"{'By ticker':<20}{'Value':>14}{'% of Book':>12}")
    for row in alloc.by_ticker:
        flag = "  ** OVER CAP **" if row.over_cap else ""
        print(f"{row.key:<20}{row.value:>14,.2f}{row.pct_of_total:>11.1f}%{flag}")
    print()
    print(f"{'By sector':<20}{'Value':>14}{'% of Book':>12}")
    for row in alloc.by_sector:
        flag = "  ** OVER CAP **" if row.over_cap else ""
        print(f"{row.key:<20}{row.value:>14,.2f}{row.pct_of_total:>11.1f}%{flag}")
    if alloc.flags:
        print("\nConcentration flags (informational):")
        for f in alloc.flags:
            print(f"  - {f}")

    g = analytics.greeks
    print(f"\nAggregate greeks")
    print("-" * 72)
    print(f"  Net delta (share-equivalent): {g.net_delta_shares:,.1f}")
    print(f"  Daily theta ($/day if nothing moves): {g.daily_theta_dollars:,.2f}")
    print(f"  Net vega ($ per 1 IV point): {g.net_vega_dollars_per_point:,.2f}")

    if analytics.iv_environment:
        print(f"\nIV environment")
        print("-" * 72)
        header = f"{'Position':<28}{'IV':>7}{'Rank':>7}{'Pctl':>7}{'Flag':>9}{'DTE':>6}{'RollWin':>9}"
        print(header)
        for row in analytics.iv_environment:
            iv = f"{row.current_iv * 100:.1f}%" if row.current_iv is not None else "n/a"
            if row.status == "building_history":
                rank = "bldg"
                pctl = "bldg"
                flag = "-"
            else:
                rank = f"{row.iv_rank:.0f}"
                pctl = f"{row.iv_percentile:.0f}"
                flag = row.rich_cheap or "-"
            dte = str(row.dte) if row.dte is not None else "-"
            near = "yes" if row.near_expiry else "-"
            print(f"{row.position_id:<28}{iv:>7}{rank:>7}{pctl:>7}{flag:>9}{dte:>6}{near:>9}")
        print(f"  (sample_size < {DEFAULT_IV_MIN_HISTORY_DAYS} days => 'bldg' = building history; "
              f"RollWin = DTE at/under the {DEFAULT_NEAR_EXPIRY_DTE}-day visibility threshold)")
