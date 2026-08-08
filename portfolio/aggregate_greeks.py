"""Portfolio-level Greek exposures rolled up across the whole book.

- net_delta_shares: share-equivalent directional exposure (options' per-share
  delta * contracts * 100, plus 1.0 delta per held share).
- daily_theta_dollars: dollar theta summed across held options -- what the
  book gains/loses per day if nothing moves. Negative for a net-long-premium
  book, positive for a net-short-premium one.
- net_vega_dollars_per_point: dollar sensitivity to a 1-point (1%) move in IV,
  summed across held options.

Shares carry delta 1 and no theta/vega.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Position
from .valuation import Valuation


@dataclass
class AggregateGreeks:
    total_value: float
    net_delta_shares: float
    daily_theta_dollars: float
    net_vega_dollars_per_point: float


def compute_aggregate_greeks(positions: list[Position], valuations: list[Valuation]) -> AggregateGreeks:
    valuation_by_id = {v.position_id: v for v in valuations}

    total_value = 0.0
    net_delta_shares = 0.0
    daily_theta_dollars = 0.0
    net_vega_dollars = 0.0

    for pos in positions:
        v = valuation_by_id.get(pos.id)
        if v is None:
            continue
        total_value += v.current_value or 0.0

        if pos.is_option:
            if v.delta is not None:
                net_delta_shares += v.delta * pos.contracts * pos.multiplier
            if v.theta is not None:
                daily_theta_dollars += v.theta * pos.contracts * pos.multiplier
            if v.vega is not None:
                net_vega_dollars += v.vega * pos.contracts * pos.multiplier
        else:
            net_delta_shares += pos.contracts

    return AggregateGreeks(
        total_value=total_value,
        net_delta_shares=net_delta_shares,
        daily_theta_dollars=daily_theta_dollars,
        net_vega_dollars_per_point=net_vega_dollars,
    )
