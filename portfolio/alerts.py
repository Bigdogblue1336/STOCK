"""Condition alerts: surfaces facts about the book. Never a trade signal --
these report state (a level was crossed, IV moved, a new strike showed up),
they do not suggest buying, selling, or any other action.

Alert types and severities:
  target_hit  (high)  mark has reached/passed target_price
  stop_hit    (high)  mark has reached/passed stop_price
  target_near (info)  progress toward target >= a configurable threshold (default 80%), not yet hit
  iv_change   (warn)  IV moved by more than a configurable % vs the prior session's snapshot
  new_expiry  (info)  a new expiry appeared in today's chain diff (see diff.py)
  new_strikes (info)  new strikes appeared within the band of a held strike (see diff.py)

These fire fresh every run based on that day's numbers (not edge-triggered on
a state transition) -- the `alerts` table's daily rows are the history; the
dashboard's "active" panel is simply today's rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import DEFAULT_IV_CHANGE_PCT, DEFAULT_TARGET_NEAR_PCT
from .diff import ChainDiff
from .models import Position
from .valuation import Valuation

SEVERITY_HIGH = "high"
SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"


@dataclass
class Alert:
    ticker: str
    alert_type: str
    severity: str
    message: str
    position_id: Optional[str] = None
    driver: Optional[str] = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _position_label(position: Position) -> str:
    if position.is_option:
        return f"{position.strike:g} {position.option_type}"
    return "shares"


def check_position_alerts(
    position: Position, valuation: Valuation, target_near_pct: float = DEFAULT_TARGET_NEAR_PCT
) -> list[Alert]:
    alerts: list[Alert] = []
    label = _position_label(position)
    mark = valuation.mark

    if mark is not None and position.target_price is not None:
        if valuation.progress_to_target_pct is not None and valuation.progress_to_target_pct >= 100:
            alerts.append(
                Alert(
                    ticker=position.ticker,
                    position_id=position.id,
                    alert_type="target_hit",
                    severity=SEVERITY_HIGH,
                    message=(
                        f"{position.ticker} {label} hit your target level "
                        f"(mark {mark:.2f} vs target {position.target_price:.2f})"
                    ),
                )
            )
        elif valuation.progress_to_target_pct is not None and valuation.progress_to_target_pct >= target_near_pct:
            alerts.append(
                Alert(
                    ticker=position.ticker,
                    position_id=position.id,
                    alert_type="target_near",
                    severity=SEVERITY_INFO,
                    message=(
                        f"{position.ticker} {label} is {valuation.progress_to_target_pct:.0f}% of the way "
                        f"to target (mark {mark:.2f}, target {position.target_price:.2f})"
                    ),
                )
            )

    if mark is not None and position.stop_price is not None:
        if valuation.progress_to_stop_pct is not None and valuation.progress_to_stop_pct >= 100:
            alerts.append(
                Alert(
                    ticker=position.ticker,
                    position_id=position.id,
                    alert_type="stop_hit",
                    severity=SEVERITY_HIGH,
                    message=(
                        f"{position.ticker} {label} hit your stop level "
                        f"(mark {mark:.2f} vs stop {position.stop_price:.2f})"
                    ),
                )
            )

    return alerts


def check_iv_change_alert(
    position: Position,
    current_iv: Optional[float],
    prior_iv: Optional[float],
    threshold_pct: float = DEFAULT_IV_CHANGE_PCT,
) -> Optional[Alert]:
    if current_iv is None or prior_iv is None or prior_iv == 0:
        return None
    pct_change = (current_iv - prior_iv) / prior_iv * 100.0
    if abs(pct_change) < threshold_pct:
        return None
    return Alert(
        ticker=position.ticker,
        position_id=position.id,
        alert_type="iv_change",
        severity=SEVERITY_WARN,
        message=(
            f"{position.ticker} {_position_label(position)} IV moved {pct_change:+.0f}% since prior session "
            f"({prior_iv * 100:.1f}% -> {current_iv * 100:.1f}%)"
        ),
    )


def build_diff_alerts(chain_diff: ChainDiff, driver: Optional[str] = None) -> list[Alert]:
    alerts: list[Alert] = []
    for expiry in chain_diff.new_expiries:
        alerts.append(
            Alert(
                ticker=chain_diff.ticker,
                alert_type="new_expiry",
                severity=SEVERITY_INFO,
                message=f"{chain_diff.ticker}: new expiry listed: {expiry}",
                driver=driver,
            )
        )
    for expiry, strikes in chain_diff.new_strikes.items():
        strikes_str = ", ".join(f"{s:g}" for s in strikes)
        alerts.append(
            Alert(
                ticker=chain_diff.ticker,
                alert_type="new_strikes",
                severity=SEVERITY_INFO,
                message=f"{chain_diff.ticker} {expiry}: new strike(s) near your position: {strikes_str}",
                driver=driver,
            )
        )
    return alerts
