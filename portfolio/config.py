"""Shared configuration/defaults for the portfolio data + valuation layer."""

from __future__ import annotations

import os

# Annual risk-free rate used in Black-Scholes greeks, as a decimal (e.g. 0.045 == 4.5%).
# Override per-run with --risk-free-rate on the CLI.
DEFAULT_RISK_FREE_RATE = 0.045

# Contract multiplier: one option contract represents this many underlying shares.
OPTION_MULTIPLIER = 100

DEFAULT_POSITIONS_FILE = os.environ.get("PORTFOLIO_POSITIONS_FILE", "positions.json")
DEFAULT_SNAPSHOT_DIR = os.environ.get("PORTFOLIO_SNAPSHOT_DIR", "snapshots")
DEFAULT_DB_PATH = os.environ.get("PORTFOLIO_DB_PATH", "portfolio.db")
DEFAULT_SECTOR_MAP_FILE = os.environ.get("PORTFOLIO_SECTOR_MAP_FILE", "sector_map.json")

# --- Portfolio layer (allocation / aggregate greeks / IV environment) ---

# Concentration caps are informational only -- they produce a flag, never a block.
DEFAULT_TICKER_CONCENTRATION_CAP = 0.40  # 40% of total portfolio value
DEFAULT_SECTOR_CONCENTRATION_CAP = 0.60  # 60% of total portfolio value

# IV rank/percentile lookback window, in calendar days, drawn from our own stored snapshots.
DEFAULT_IV_LOOKBACK_DAYS = 252
# Below this many sampled days of IV history for a contract, report "building history" instead of a rank.
DEFAULT_IV_MIN_HISTORY_DAYS = 20
DEFAULT_IV_RICH_THRESHOLD = 70.0  # IV rank above this => "rich"
DEFAULT_IV_CHEAP_THRESHOLD = 30.0  # IV rank below this => "cheap"

# DTE at/under which a position is flagged for time-decay/roll visibility (fact only, no advice).
DEFAULT_NEAR_EXPIRY_DTE = 45

# --- Market context layer (macro gate + daily news) ---

# Deterministic 0-100 macro gate: same data in, same score out. Component weights must sum to 1.0.
DEFAULT_MACRO_LOOKBACK_DAYS = 252
DEFAULT_MACRO_WEIGHTS = {
    "vix_level": 0.25,
    "vix_term_structure": 0.25,
    "breadth": 0.25,
    "credit_spread": 0.25,
}
DEFAULT_VIX_TICKER = "^VIX"
DEFAULT_VIX3M_TICKER = "^VIX3M"
DEFAULT_CREDIT_HY_TICKER = "HYG"
DEFAULT_CREDIT_SAFE_TICKER = "TLT"
# Breadth proxy: the 11 SPDR sector ETFs standing in for "% of SPY constituents above 200dma"
# (pulling + computing 200dma for ~500 individual constituents daily isn't practical here).
# Override with --breadth-tickers if you have a real constituent list to feed in.
DEFAULT_BREADTH_TICKERS = [
    "XLK", "XLF", "XLE", "XLY", "XLP", "XLI", "XLB", "XLU", "XLV", "XLRE", "XLC",
]

# Daily Claude news analysis: informational only, never a trade signal.
DEFAULT_NEWS_WINDOW_DAYS = 3
DEFAULT_NEWS_MODEL = os.environ.get("PORTFOLIO_NEWS_MODEL", "claude-haiku-4-5-20251001")
DEFAULT_NEWS_MAX_TOKENS = 700
