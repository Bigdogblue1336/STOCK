"""Orchestrates one valuation run: pull market data, snapshot chains, compute
greeks + valuation, roll up portfolio-level allocation/greeks/IV analytics,
persist everything to SQLite, and print a summary.

    python -m portfolio.cli [--positions positions.json] [--db portfolio.db]
                             [--snapshot-dir snapshots] [--asof 2026-08-08]
                             [--risk-free-rate 0.045] [--sector-map sector_map.json]
                             [--ticker-cap 0.40] [--sector-cap 0.60]
                             [--iv-lookback-days 252] [--iv-min-history-days 20]
                             [--iv-rich-threshold 70] [--iv-cheap-threshold 30]
                             [--near-expiry-dte 45]

--asof stamps the run's asof_date for storage/diffing. Marks always come
from the live feed pulled right now -- this does not reconstruct historical
chains for a past date.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime

from . import config, db as db_module
from .analytics import PortfolioAnalytics, compute_portfolio_analytics, print_analytics_report, save_portfolio_analytics
from .black_scholes import compute_greeks
from .market_data import (
    MarketDataError,
    get_full_chain_snapshot,
    get_option_market_data,
    get_underlying_quote,
)
from .models import Position, load_positions_file
from .sectors import load_sector_map
from .snapshot import write_snapshot
from .valuation import Valuation, compute_valuation

logger = logging.getLogger(__name__)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Options + equity portfolio data/valuation run")
    p.add_argument("--positions", default=config.DEFAULT_POSITIONS_FILE, help="Path to positions.json")
    p.add_argument("--db", default=config.DEFAULT_DB_PATH, help="Path to SQLite database")
    p.add_argument("--snapshot-dir", default=config.DEFAULT_SNAPSHOT_DIR, help="Directory for dated chain snapshots")
    p.add_argument(
        "--asof",
        default=None,
        help="YYYY-MM-DD to stamp this run's stored data for later diffing (default: today). "
        "Marks still come from the live feed; this does not reconstruct a historical chain.",
    )
    p.add_argument(
        "--risk-free-rate",
        type=float,
        default=config.DEFAULT_RISK_FREE_RATE,
        help=f"Annual risk-free rate as a decimal (default {config.DEFAULT_RISK_FREE_RATE})",
    )
    p.add_argument(
        "--sector-map", default=config.DEFAULT_SECTOR_MAP_FILE, help="Path to ticker->sector config JSON"
    )
    p.add_argument(
        "--ticker-cap", type=float, default=config.DEFAULT_TICKER_CONCENTRATION_CAP,
        help=f"Informational concentration cap per ticker, as a fraction (default {config.DEFAULT_TICKER_CONCENTRATION_CAP})",
    )
    p.add_argument(
        "--sector-cap", type=float, default=config.DEFAULT_SECTOR_CONCENTRATION_CAP,
        help=f"Informational concentration cap per sector, as a fraction (default {config.DEFAULT_SECTOR_CONCENTRATION_CAP})",
    )
    p.add_argument(
        "--iv-lookback-days", type=int, default=config.DEFAULT_IV_LOOKBACK_DAYS,
        help=f"IV rank/percentile lookback window in days (default {config.DEFAULT_IV_LOOKBACK_DAYS})",
    )
    p.add_argument(
        "--iv-min-history-days", type=int, default=config.DEFAULT_IV_MIN_HISTORY_DAYS,
        help=f"Minimum sampled days before IV rank/percentile is reported (default {config.DEFAULT_IV_MIN_HISTORY_DAYS})",
    )
    p.add_argument(
        "--iv-rich-threshold", type=float, default=config.DEFAULT_IV_RICH_THRESHOLD,
        help=f"IV rank above which a position is flagged rich (default {config.DEFAULT_IV_RICH_THRESHOLD})",
    )
    p.add_argument(
        "--iv-cheap-threshold", type=float, default=config.DEFAULT_IV_CHEAP_THRESHOLD,
        help=f"IV rank below which a position is flagged cheap (default {config.DEFAULT_IV_CHEAP_THRESHOLD})",
    )
    p.add_argument(
        "--near-expiry-dte", type=int, default=config.DEFAULT_NEAR_EXPIRY_DTE,
        help=f"DTE at/under which a position is flagged for time-decay/roll visibility (default {config.DEFAULT_NEAR_EXPIRY_DTE})",
    )
    return p.parse_args(argv)


def _group_by_ticker(positions: list[Position]) -> dict[str, list[Position]]:
    grouped: dict[str, list[Position]] = {}
    for pos in positions:
        grouped.setdefault(pos.ticker, []).append(pos)
    return grouped


def value_position(
    position: Position,
    quote: dict,
    chain_snapshot: dict | None,
    asof: date,
    risk_free_rate: float,
) -> Valuation:
    if position.is_option:
        market = get_option_market_data(
            chain_snapshot, position.expiry, position.option_type, position.strike
        )
        dte = position.dte(asof)
        greeks_obj = compute_greeks(
            option_type=position.option_type,
            spot=quote["mark"],
            strike=position.strike,
            days_to_expiry=dte,
            risk_free_rate=risk_free_rate,
            iv=market.get("iv"),
        )
        greeks = greeks_obj.to_dict()
    else:
        market = {"mark": quote["mark"], "bid": quote.get("bid"), "ask": quote.get("ask"), "last": quote.get("last")}
        greeks = None

    return compute_valuation(position, market, asof, greeks)


def run(argv=None) -> tuple[list[Valuation], PortfolioAnalytics]:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)

    asof = datetime.strptime(args.asof, "%Y-%m-%d").date() if args.asof else date.today()
    run_timestamp = datetime.utcnow().isoformat() + "Z"

    positions = load_positions_file(args.positions)
    if not positions:
        logger.warning("No positions found in %s", args.positions)
        return [], None

    grouped = _group_by_ticker(positions)
    valuations: list[Valuation] = []
    sector_map = load_sector_map(args.sector_map)

    with db_module.open_db(args.db) as conn:
        for position in positions:
            db_module.upsert_position(conn, position, updated_at=run_timestamp)

        for ticker, ticker_positions in grouped.items():
            has_options = any(p.is_option for p in ticker_positions)
            try:
                quote = get_underlying_quote(ticker)
            except MarketDataError as exc:
                logger.error("Skipping %s: %s", ticker, exc)
                continue

            chain_snapshot = None
            if has_options:
                try:
                    chain_snapshot = get_full_chain_snapshot(ticker, asof)
                    write_snapshot(conn, args.snapshot_dir, ticker, asof, chain_snapshot)
                except MarketDataError as exc:
                    logger.error("Could not pull option chain for %s: %s", ticker, exc)

            for position in ticker_positions:
                if position.is_option and chain_snapshot is None:
                    logger.error("Skipping %s: no option chain available", position.id)
                    continue
                try:
                    valuation = value_position(
                        position, quote, chain_snapshot, asof, args.risk_free_rate
                    )
                except MarketDataError as exc:
                    logger.error("Skipping %s: %s", position.id, exc)
                    continue

                row = valuation.to_dict()
                row["run_timestamp"] = run_timestamp
                db_module.insert_valuation(conn, row)
                valuations.append(valuation)

        valued_positions = [p for p in positions if p.id in {v.position_id for v in valuations}]
        analytics = compute_portfolio_analytics(
            conn,
            valued_positions,
            valuations,
            asof,
            sector_map=sector_map,
            ticker_cap=args.ticker_cap,
            sector_cap=args.sector_cap,
            iv_lookback_days=args.iv_lookback_days,
            iv_min_history_days=args.iv_min_history_days,
            iv_rich_threshold=args.iv_rich_threshold,
            iv_cheap_threshold=args.iv_cheap_threshold,
            near_expiry_dte=args.near_expiry_dte,
        )
        save_portfolio_analytics(conn, asof.isoformat(), run_timestamp, analytics)

    print_report(valuations, asof)
    print_analytics_report(analytics)
    return valuations, analytics


def print_report(valuations: list[Valuation], asof: date) -> None:
    print(f"\nPortfolio valuation as of {asof.isoformat()}")
    print("-" * 100)
    header = f"{'ID':<28}{'Mark':>9}{'Value':>12}{'P&L $':>12}{'P&L %':>9}{'DTE':>6}{'->Tgt%':>9}{'->Stop%':>9}"
    print(header)
    print("-" * 100)
    total_value = 0.0
    total_pnl = 0.0
    for v in valuations:
        mark = f"{v.mark:.2f}" if v.mark is not None else "n/a"
        value = f"{v.current_value:,.2f}" if v.current_value is not None else "n/a"
        pnl = f"{v.unrealized_pnl:,.2f}" if v.unrealized_pnl is not None else "n/a"
        pnl_pct = f"{v.unrealized_pnl_pct:.1f}" if v.unrealized_pnl_pct is not None else "n/a"
        dte = str(v.dte) if v.dte is not None else "-"
        tgt = f"{v.progress_to_target_pct:.0f}" if v.progress_to_target_pct is not None else "-"
        stop = f"{v.progress_to_stop_pct:.0f}" if v.progress_to_stop_pct is not None else "-"
        print(f"{v.position_id:<28}{mark:>9}{value:>12}{pnl:>12}{pnl_pct:>9}{dte:>6}{tgt:>9}{stop:>9}")
        total_value += v.current_value or 0.0
        total_pnl += v.unrealized_pnl or 0.0
    print("-" * 100)
    print(f"{'TOTAL':<28}{'':>9}{total_value:>12,.2f}{total_pnl:>12,.2f}")


if __name__ == "__main__":
    run()
