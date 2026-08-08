"""Deterministic macro gate: a 0-100 blend of four market-environment signals.

Same data in, same score out -- no LLM, no randomness. This describes the
general environment the book sits in; it is not a per-position signal.

Components (each normalized to 0-100, higher = more risk-on/favorable):
  vix_level          100 - (current VIX's percentile rank within its trailing
                      lookback window). High VIX percentile = fear = low score.
  vix_term_structure 100 - (percentile rank of the VIX/VIX3M ratio). A ratio
                      > 1 (backwardation) signals stress; scored the same way
                      as vix_level so higher = calmer term structure.
  breadth            % of a basket of proxy tickers trading above their own
                      200-day moving average (see config.DEFAULT_BREADTH_TICKERS
                      for the "% of SPY constituents above 200dma" proxy used).
  credit_spread      Percentile rank of the current HYG/TLT price ratio within
                      its trailing lookback window. Higher ratio (high-yield
                      credit outperforming Treasuries) = risk-on = higher score.

The composite score is the weighted average of whichever components could be
computed; weights (which must sum to 1.0) are renormalized over the available
components if any one component's data pull fails, so a single bad fetch
degrades the score gracefully rather than aborting the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .config import (
    DEFAULT_BREADTH_TICKERS,
    DEFAULT_CREDIT_HY_TICKER,
    DEFAULT_CREDIT_SAFE_TICKER,
    DEFAULT_MACRO_LOOKBACK_DAYS,
    DEFAULT_MACRO_WEIGHTS,
    DEFAULT_VIX3M_TICKER,
    DEFAULT_VIX_TICKER,
)
from .market_data import MarketDataError, get_price_history

logger = logging.getLogger(__name__)

REQUIRED_COMPONENTS = ("vix_level", "vix_term_structure", "breadth", "credit_spread")


class MacroGateError(ValueError):
    pass


@dataclass
class MacroComponent:
    name: str
    raw_value: Optional[float]
    score: Optional[float]  # 0-100, None if data unavailable
    weight: float
    detail: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class MacroGate:
    asof_date: str
    composite_score: Optional[float]
    components: list[MacroComponent] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)


def validate_weights(weights: dict[str, float]) -> None:
    missing = [k for k in REQUIRED_COMPONENTS if k not in weights]
    if missing:
        raise MacroGateError(f"Missing macro gate weight(s): {missing}")
    total = sum(weights[k] for k in REQUIRED_COMPONENTS)
    if abs(total - 1.0) > 1e-6:
        raise MacroGateError(f"Macro gate weights must sum to 1.0, got {total}")


def _percentile_rank(current: float, history: list[float]) -> float:
    """% of historical values <= current, 0-100."""
    if not history:
        return 50.0
    return sum(1 for v in history if v <= current) / len(history) * 100.0


def _closes(records: list[dict]) -> list[float]:
    return [r["close"] for r in records]


def _trailing(values: list[float], lookback_days: int) -> tuple[float, list[float]]:
    """Split a close series into (current, trailing history excluding current)."""
    if not values:
        raise MacroGateError("empty price series")
    current = values[-1]
    history = values[-(lookback_days + 1):-1] if len(values) > 1 else []
    return current, history


def _compute_vix_level(ticker: str, lookback_days: int, weight: float) -> MacroComponent:
    name = "vix_level"
    try:
        closes = _closes(get_price_history(ticker, period="2y"))
        current, history = _trailing(closes, lookback_days)
        pct = _percentile_rank(current, history)
        score = 100.0 - pct
        detail = f"{ticker}={current:.2f}, {pct:.0f}th pct of trailing {len(history)}d"
        return MacroComponent(name=name, raw_value=current, score=score, weight=weight, detail=detail)
    except MarketDataError as exc:
        logger.warning("macro gate: %s unavailable: %s", name, exc)
        return MacroComponent(name=name, raw_value=None, score=None, weight=weight, detail=str(exc))


def _compute_vix_term_structure(
    vix_ticker: str, vix3m_ticker: str, lookback_days: int, weight: float
) -> MacroComponent:
    name = "vix_term_structure"
    try:
        vix = {r["date"]: r["close"] for r in get_price_history(vix_ticker, period="2y")}
        vix3m = {r["date"]: r["close"] for r in get_price_history(vix3m_ticker, period="2y")}
        common_dates = sorted(set(vix) & set(vix3m))
        if not common_dates:
            raise MarketDataError(f"no overlapping dates between {vix_ticker} and {vix3m_ticker}")
        ratios = [vix[d] / vix3m[d] for d in common_dates if vix3m[d]]
        current, history = _trailing(ratios, lookback_days)
        pct = _percentile_rank(current, history)
        score = 100.0 - pct
        detail = f"{vix_ticker}/{vix3m_ticker}={current:.3f}, {pct:.0f}th pct of trailing {len(history)}d"
        return MacroComponent(name=name, raw_value=current, score=score, weight=weight, detail=detail)
    except MarketDataError as exc:
        logger.warning("macro gate: %s unavailable: %s", name, exc)
        return MacroComponent(name=name, raw_value=None, score=None, weight=weight, detail=str(exc))


def _compute_breadth(tickers: list[str], weight: float) -> MacroComponent:
    name = "breadth"
    above = 0
    total = 0
    failures = []
    for t in tickers:
        try:
            closes = _closes(get_price_history(t, period="2y"))
            if len(closes) < 200:
                failures.append(f"{t}: insufficient history ({len(closes)}d)")
                continue
            sma200 = sum(closes[-200:]) / 200.0
            total += 1
            if closes[-1] > sma200:
                above += 1
        except MarketDataError as exc:
            failures.append(f"{t}: {exc}")

    if total == 0:
        detail = "; ".join(failures) or "no breadth basket data available"
        logger.warning("macro gate: %s unavailable: %s", name, detail)
        return MacroComponent(name=name, raw_value=None, score=None, weight=weight, detail=detail)

    pct = above / total * 100.0
    detail = f"{above}/{total} basket tickers above 200dma"
    if failures:
        detail += f" ({len(failures)} skipped: {'; '.join(failures)})"
    return MacroComponent(name=name, raw_value=pct, score=pct, weight=weight, detail=detail)


def _compute_credit_spread(
    hy_ticker: str, safe_ticker: str, lookback_days: int, weight: float
) -> MacroComponent:
    name = "credit_spread"
    try:
        hy = {r["date"]: r["close"] for r in get_price_history(hy_ticker, period="2y")}
        safe = {r["date"]: r["close"] for r in get_price_history(safe_ticker, period="2y")}
        common_dates = sorted(set(hy) & set(safe))
        if not common_dates:
            raise MarketDataError(f"no overlapping dates between {hy_ticker} and {safe_ticker}")
        ratios = [hy[d] / safe[d] for d in common_dates if safe[d]]
        current, history = _trailing(ratios, lookback_days)
        pct = _percentile_rank(current, history)
        detail = f"{hy_ticker}/{safe_ticker}={current:.3f}, {pct:.0f}th pct of trailing {len(history)}d"
        return MacroComponent(name=name, raw_value=current, score=pct, weight=weight, detail=detail)
    except MarketDataError as exc:
        logger.warning("macro gate: %s unavailable: %s", name, exc)
        return MacroComponent(name=name, raw_value=None, score=None, weight=weight, detail=str(exc))


def compute_macro_gate(
    asof: date,
    weights: Optional[dict[str, float]] = None,
    lookback_days: int = DEFAULT_MACRO_LOOKBACK_DAYS,
    breadth_tickers: Optional[list[str]] = None,
    vix_ticker: str = DEFAULT_VIX_TICKER,
    vix3m_ticker: str = DEFAULT_VIX3M_TICKER,
    credit_hy_ticker: str = DEFAULT_CREDIT_HY_TICKER,
    credit_safe_ticker: str = DEFAULT_CREDIT_SAFE_TICKER,
) -> MacroGate:
    weights = dict(weights) if weights else dict(DEFAULT_MACRO_WEIGHTS)
    validate_weights(weights)
    breadth_tickers = breadth_tickers or DEFAULT_BREADTH_TICKERS

    components = [
        _compute_vix_level(vix_ticker, lookback_days, weights["vix_level"]),
        _compute_vix_term_structure(vix_ticker, vix3m_ticker, lookback_days, weights["vix_term_structure"]),
        _compute_breadth(breadth_tickers, weights["breadth"]),
        _compute_credit_spread(credit_hy_ticker, credit_safe_ticker, lookback_days, weights["credit_spread"]),
    ]

    available = [c for c in components if c.score is not None]
    if not available:
        composite = None
    else:
        weight_sum = sum(c.weight for c in available)
        composite = sum(c.score * c.weight for c in available) / weight_sum if weight_sum else None

    return MacroGate(asof_date=asof.isoformat(), composite_score=composite, components=components, weights=weights)
