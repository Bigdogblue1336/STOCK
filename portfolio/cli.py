"""Orchestrates one valuation run: pull market data, snapshot chains, compute
greeks + valuation, roll up portfolio-level allocation/greeks/IV analytics,
score the deterministic macro gate, run daily Claude news analysis per held
name, persist everything to SQLite, and print a summary.

    python -m portfolio.cli [--positions positions.json] [--db portfolio.db]
                             [--snapshot-dir snapshots] [--asof 2026-08-08]
                             [--risk-free-rate 0.045] [--sector-map sector_map.json]
                             [--ticker-cap 0.40] [--sector-cap 0.60]
                             [--iv-lookback-days 252] [--iv-min-history-days 20]
                             [--iv-rich-threshold 70] [--iv-cheap-threshold 30]
                             [--near-expiry-dte 45]
                             [--macro-weights '{"vix_level":0.25,...}']
                             [--macro-lookback-days 252] [--breadth-tickers XLK,XLF,...]
                             [--news-window-days 3] [--news-model claude-haiku-4-5-20251001]
                             [--skip-news]

--asof stamps the run's asof_date for storage/diffing. Marks always come
from the live feed pulled right now -- this does not reconstruct historical
chains for a past date.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime

from . import config, db as db_module, notifier as notifier_module
from .alerts import Alert, build_diff_alerts, check_iv_change_alert, check_position_alerts
from .analytics import PortfolioAnalytics, compute_portfolio_analytics, print_analytics_report, save_portfolio_analytics
from .black_scholes import compute_greeks
from .diff import ChainDiff, compute_diff
from .macro_gate import MacroGate, MacroGateError, compute_macro_gate
from .market_data import (
    MarketDataError,
    find_contract,
    get_full_chain_snapshot,
    get_option_market_data,
    get_underlying_quote,
)
from .models import Position, load_positions_file
from .news import NewsAnalysis, build_client, get_or_create_news_analysis
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
    p.add_argument(
        "--macro-weights", type=str, default=None,
        help='JSON object of macro gate component weights, must sum to 1.0 '
        '(default {"vix_level":0.25,"vix_term_structure":0.25,"breadth":0.25,"credit_spread":0.25})',
    )
    p.add_argument(
        "--macro-lookback-days", type=int, default=config.DEFAULT_MACRO_LOOKBACK_DAYS,
        help=f"Percentile lookback window for the macro gate, in days (default {config.DEFAULT_MACRO_LOOKBACK_DAYS})",
    )
    p.add_argument(
        "--breadth-tickers", type=str, default=None,
        help="Comma-separated breadth proxy basket (default: 11 SPDR sector ETFs)",
    )
    p.add_argument("--vix-ticker", default=config.DEFAULT_VIX_TICKER)
    p.add_argument("--vix3m-ticker", default=config.DEFAULT_VIX3M_TICKER)
    p.add_argument("--credit-hy-ticker", default=config.DEFAULT_CREDIT_HY_TICKER)
    p.add_argument("--credit-safe-ticker", default=config.DEFAULT_CREDIT_SAFE_TICKER)
    p.add_argument(
        "--news-window-days", type=int, default=config.DEFAULT_NEWS_WINDOW_DAYS,
        help=f"Headline lookback window for news analysis, in days (default {config.DEFAULT_NEWS_WINDOW_DAYS})",
    )
    p.add_argument(
        "--news-model", default=config.DEFAULT_NEWS_MODEL,
        help=f"Claude model for news analysis (default {config.DEFAULT_NEWS_MODEL})",
    )
    p.add_argument(
        "--skip-news", action="store_true",
        help="Skip the Claude news analysis phase entirely (no API calls, no charges)",
    )
    p.add_argument(
        "--strike-band", type=float, default=config.DEFAULT_STRIKE_BAND,
        help=f"New-strike detection band around held strikes, as a fraction (default {config.DEFAULT_STRIKE_BAND})",
    )
    p.add_argument(
        "--target-near-pct", type=float, default=config.DEFAULT_TARGET_NEAR_PCT,
        help=f"progress_to_target_pct at/above which a target_near alert fires (default {config.DEFAULT_TARGET_NEAR_PCT})",
    )
    p.add_argument(
        "--iv-change-pct", type=float, default=config.DEFAULT_IV_CHANGE_PCT,
        help=f"Absolute relative IV move vs prior session that triggers an iv_change alert (default {config.DEFAULT_IV_CHANGE_PCT})",
    )
    p.add_argument(
        "--notifier", default=config.DEFAULT_NOTIFIER_CHANNEL, choices=["none", "telegram", "imessage"],
        help="Push today's alerts to this channel (default: none / off)",
    )
    p.add_argument(
        "--imessage-recipient", default=config.DEFAULT_IMESSAGE_RECIPIENT,
        help="macOS Messages buddy id (phone/email) for the imessage notifier channel",
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


def run(
    argv=None,
) -> tuple[list[Valuation], PortfolioAnalytics, MacroGate, list[NewsAnalysis], list[Alert]]:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)

    asof = datetime.strptime(args.asof, "%Y-%m-%d").date() if args.asof else date.today()
    run_timestamp = datetime.utcnow().isoformat() + "Z"

    macro_weights = json.loads(args.macro_weights) if args.macro_weights else None
    breadth_tickers = (
        [t.strip().upper() for t in args.breadth_tickers.split(",") if t.strip()]
        if args.breadth_tickers
        else None
    )

    positions = load_positions_file(args.positions)
    if not positions:
        logger.warning("No positions found in %s", args.positions)
        positions = []

    grouped = _group_by_ticker(positions)
    valuations: list[Valuation] = []
    sector_map = load_sector_map(args.sector_map)
    chain_diffs: dict[str, ChainDiff] = {}
    iv_change_alerts: list[Alert] = []

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
            prior_snapshot = None
            if has_options:
                prior_snapshot = db_module.get_prior_snapshot(conn, ticker, asof.isoformat())
                try:
                    chain_snapshot = get_full_chain_snapshot(ticker, asof)
                    write_snapshot(conn, args.snapshot_dir, ticker, asof, chain_snapshot)

                    held_strikes = [p.strike for p in ticker_positions if p.is_option and p.strike]
                    diff_result = compute_diff(
                        ticker,
                        prior_snapshot["data"] if prior_snapshot else None,
                        chain_snapshot,
                        held_strikes,
                        strike_band=args.strike_band,
                    )
                    chain_diffs[ticker] = diff_result
                    db_module.save_chain_diff(
                        conn, asof.isoformat(), run_timestamp, ticker,
                        diff_result.new_expiries, diff_result.new_strikes,
                    )
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

                if position.is_option and prior_snapshot is not None:
                    prior_contract = find_contract(
                        prior_snapshot["data"], position.expiry, position.option_type, position.strike
                    )
                    prior_iv = prior_contract.get("iv") if prior_contract else None
                    iv_alert = check_iv_change_alert(
                        position, valuation.iv, prior_iv, threshold_pct=args.iv_change_pct
                    )
                    if iv_alert:
                        iv_change_alerts.append(iv_alert)

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

        try:
            macro_gate = compute_macro_gate(
                asof,
                weights=macro_weights,
                lookback_days=args.macro_lookback_days,
                breadth_tickers=breadth_tickers,
                vix_ticker=args.vix_ticker,
                vix3m_ticker=args.vix3m_ticker,
                credit_hy_ticker=args.credit_hy_ticker,
                credit_safe_ticker=args.credit_safe_ticker,
            )
        except MacroGateError as exc:
            logger.error("Macro gate configuration error: %s", exc)
            raise
        db_module.insert_macro_gate(
            conn, asof.isoformat(), run_timestamp, macro_gate.composite_score,
            macro_gate.weights, [c.to_dict() for c in macro_gate.components],
        )

        news_results: list[NewsAnalysis] = []
        if args.skip_news:
            logger.info("News analysis skipped (--skip-news)")
        else:
            client = build_client()
            if client is not None:
                for ticker, ticker_positions in grouped.items():
                    try:
                        result = get_or_create_news_analysis(
                            conn, client, ticker, ticker_positions, asof, run_timestamp,
                            window_days=args.news_window_days, model=args.news_model,
                        )
                        news_results.append(result)
                    except Exception as exc:
                        logger.error("News analysis failed for %s: %s", ticker, exc)

        news_by_ticker = {n.ticker: n for n in news_results}
        valuation_by_id = {v.position_id: v for v in valuations}

        alerts: list[Alert] = list(iv_change_alerts)
        for position in valued_positions:
            alerts.extend(
                check_position_alerts(position, valuation_by_id[position.id], target_near_pct=args.target_near_pct)
            )
        for ticker, diff_result in chain_diffs.items():
            if diff_result.is_empty():
                continue
            news = news_by_ticker.get(ticker)
            driver = news.key_drivers[0] if news and news.key_drivers else None
            alerts.extend(build_diff_alerts(diff_result, driver=driver))

        severity_order = {"high": 0, "warn": 1, "info": 2}
        alerts.sort(key=lambda a: (severity_order.get(a.severity, 9), a.ticker))

        for alert in alerts:
            db_module.insert_alert(conn, asof.isoformat(), run_timestamp, alert.to_dict())

    print_report(valuations, asof)
    print_analytics_report(analytics)
    print_macro_gate_report(macro_gate)
    print_news_report(news_results)
    print_alerts_report(alerts)

    sent = notifier_module.maybe_send(
        [a.to_dict() for a in alerts], asof, channel=args.notifier, imessage_recipient=args.imessage_recipient
    )
    if sent:
        logger.info("Alerts pushed via %s notifier", args.notifier)

    return valuations, analytics, macro_gate, news_results, alerts


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


def print_macro_gate_report(macro_gate: MacroGate) -> None:
    print(f"\nMacro gate as of {macro_gate.asof_date} (deterministic, 0-100; general environment only)")
    print("-" * 72)
    score_str = f"{macro_gate.composite_score:.1f}" if macro_gate.composite_score is not None else "n/a"
    print(f"  Composite score: {score_str}")
    for c in macro_gate.components:
        score_str = f"{c.score:.1f}" if c.score is not None else "n/a"
        print(f"    {c.name:<20} score={score_str:<6} weight={c.weight:.2f}  {c.detail}")


def print_news_report(news_results: list[NewsAnalysis]) -> None:
    if not news_results:
        return
    print(f"\nDaily news analysis (Claude; informational only, not a trade signal)")
    print("-" * 72)
    for n in news_results:
        cache_note = " (cached)" if n.from_cache else ""
        flag_note = "  ** POSITION FLAG **" if n.position_flag else ""
        print(f"\n  {n.ticker} [{n.sentiment}]{cache_note}{flag_note}")
        print(f"    {n.summary}")
        if n.key_drivers:
            print(f"    Drivers: {', '.join(n.key_drivers)}")
        if n.position_flag and n.position_flag_detail:
            print(f"    Position note: {n.position_flag_detail}")


def print_alerts_report(alerts: list[Alert]) -> None:
    print(f"\nAlerts (facts about the book; never a trade recommendation)")
    print("-" * 72)
    if not alerts:
        print("  (none)")
        return
    for a in alerts:
        driver_note = f"  [driver: {a.driver}]" if a.driver else ""
        print(f"  [{a.severity.upper():<4}] {a.alert_type:<12} {a.message}{driver_note}")


if __name__ == "__main__":
    run()
