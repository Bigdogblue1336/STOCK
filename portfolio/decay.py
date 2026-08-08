"""Illustrative theta-decay curves for the dashboard.

Holds spot and IV constant at their current values and reprices each held
option via Black-Scholes as days-to-expiry counts down to zero. This is NOT
a forecast -- spot and IV will move in reality -- it's a "what does time
alone do to this position's extrinsic value" illustration, which is what the
spec asks for ("BS at constant spot+IV, illustrative not predictive").
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from .black_scholes import bs_price
from .config import DEFAULT_RISK_FREE_RATE
from .models import Position


def _intrinsic(option_type: str, spot: float, strike: float) -> float:
    return max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)


def position_decay_curve(
    position: Position,
    spot: float,
    iv: float,
    asof: date,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> list[dict]:
    """One row per day from today's DTE down to 0 for a single option position."""
    if not position.is_option:
        return []
    dte0 = position.dte(asof)
    if dte0 is None or dte0 <= 0 or iv is None or iv <= 0 or spot is None or spot <= 0:
        return []

    intrinsic = _intrinsic(position.option_type, spot, position.strike)
    multiplier = position.contracts * position.multiplier

    curve = []
    for days_left in range(dte0, -1, -1):
        price = bs_price(position.option_type, spot, position.strike, days_left, risk_free_rate, iv)
        extrinsic_per_share = max((price or intrinsic) - intrinsic, 0.0)
        curve.append(
            {
                "position_id": position.id,
                "ticker": position.ticker,
                "date": (asof + timedelta(days=(dte0 - days_left))).isoformat(),
                "dte": days_left,
                "extrinsic_per_share": extrinsic_per_share,
                "extrinsic_dollars": extrinsic_per_share * multiplier,
            }
        )
    return curve


def portfolio_decay_projection(
    positions: list[Position],
    spot_by_ticker: dict[str, float],
    iv_by_position_id: dict[str, Optional[float]],
    asof: date,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> list[dict]:
    """Concatenated per-day, per-position rows across every held option, out to
    the longest-dated position's expiry. The dashboard stacks these by position_id."""
    rows: list[dict] = []
    for position in positions:
        if not position.is_option:
            continue
        spot = spot_by_ticker.get(position.ticker)
        iv = iv_by_position_id.get(position.id)
        if spot is None or iv is None:
            continue
        rows.extend(position_decay_curve(position, spot, iv, asof, risk_free_rate))
    return rows
