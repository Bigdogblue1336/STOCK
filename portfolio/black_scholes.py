"""Black-Scholes option greeks, computed locally (stdlib math only, no scipy).

Assumes a non-dividend-paying underlying. Inputs:
  S      spot price
  K      strike
  T      time to expiry, in years (actual days / 365)
  r      annual risk-free rate, decimal (e.g. 0.045)
  sigma  implied volatility, decimal (e.g. 0.32)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

DAYS_PER_YEAR = 365.0


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class Greeks:
    delta: Optional[float]
    gamma: Optional[float]
    theta: Optional[float]  # per calendar day
    vega: Optional[float]  # per 1 percentage-point (0.01) change in IV

    def to_dict(self) -> dict:
        return {"delta": self.delta, "gamma": self.gamma, "theta": self.theta, "vega": self.vega}


def days_to_years(days: int) -> float:
    return days / DAYS_PER_YEAR


def compute_greeks(
    option_type: str,
    spot: float,
    strike: float,
    days_to_expiry: int,
    risk_free_rate: float,
    iv: Optional[float],
) -> Greeks:
    """Compute delta/gamma/theta/vega for one option contract (per-share terms).

    Returns all-None greeks if inputs are degenerate (expired, no IV, non-positive
    spot/strike) since Black-Scholes is undefined there.
    """
    if (
        iv is None
        or iv <= 0
        or spot is None
        or spot <= 0
        or strike is None
        or strike <= 0
        or days_to_expiry is None
        or days_to_expiry <= 0
    ):
        return Greeks(delta=None, gamma=None, theta=None, vega=None)

    option_type = option_type.lower()
    T = days_to_years(days_to_expiry)
    sigma = iv
    r = risk_free_rate

    sqrt_T = math.sqrt(T)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    pdf_d1 = _norm_pdf(d1)
    cdf_d1 = _norm_cdf(d1)
    cdf_d2 = _norm_cdf(d2)

    gamma = pdf_d1 / (spot * sigma * sqrt_T)
    vega = (spot * pdf_d1 * sqrt_T) / 100.0  # per 1 vol point (1%)

    if option_type == "call":
        delta = cdf_d1
        theta_annual = (
            -(spot * pdf_d1 * sigma) / (2.0 * sqrt_T)
            - r * strike * math.exp(-r * T) * cdf_d2
        )
    elif option_type == "put":
        delta = cdf_d1 - 1.0
        theta_annual = (
            -(spot * pdf_d1 * sigma) / (2.0 * sqrt_T)
            + r * strike * math.exp(-r * T) * _norm_cdf(-d2)
        )
    else:
        raise ValueError(f"option_type must be 'call' or 'put', got {option_type!r}")

    theta_per_day = theta_annual / DAYS_PER_YEAR

    return Greeks(delta=delta, gamma=gamma, theta=theta_per_day, vega=vega)
