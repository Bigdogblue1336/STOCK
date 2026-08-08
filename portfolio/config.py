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
