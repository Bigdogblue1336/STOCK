"""Data + valuation layer for the options/equity portfolio monitor.

Later layers (alerting, reporting, dashboards) should import from here
rather than reaching into individual modules directly where practical:

    from portfolio import Position, load_positions_file, compute_valuation
"""

from .aggregate_greeks import AggregateGreeks, compute_aggregate_greeks
from .allocation import AllocationResult, compute_allocation
from .alerts import Alert, build_diff_alerts, check_iv_change_alert, check_position_alerts
from .analytics import PortfolioAnalytics, compute_portfolio_analytics
from .black_scholes import Greeks, compute_greeks
from .decay import portfolio_decay_projection, position_decay_curve
from .diff import ChainDiff, compute_diff
from .iv_environment import IVEnvironment, compute_iv_environment
from .macro_gate import MacroGate, MacroGateError, compute_macro_gate
from .models import Position, PositionError, load_positions, load_positions_file
from .news import NewsAnalysis, NewsAnalysisError, get_or_create_news_analysis
from .notifier import maybe_send
from .valuation import Valuation, compute_valuation

__all__ = [
    "Position",
    "PositionError",
    "load_positions",
    "load_positions_file",
    "Greeks",
    "compute_greeks",
    "Valuation",
    "compute_valuation",
    "AllocationResult",
    "compute_allocation",
    "AggregateGreeks",
    "compute_aggregate_greeks",
    "IVEnvironment",
    "compute_iv_environment",
    "PortfolioAnalytics",
    "compute_portfolio_analytics",
    "MacroGate",
    "MacroGateError",
    "compute_macro_gate",
    "NewsAnalysis",
    "NewsAnalysisError",
    "get_or_create_news_analysis",
    "ChainDiff",
    "compute_diff",
    "Alert",
    "check_position_alerts",
    "check_iv_change_alert",
    "build_diff_alerts",
    "maybe_send",
    "position_decay_curve",
    "portfolio_decay_projection",
]
