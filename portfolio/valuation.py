"""Per-position valuation: mark, current value, P&L, DTE, progress to target/stop.

Progress-to-target/stop is measured as the fraction of the entry->target (or
entry->stop) per-share distance that the current mark has covered:

    progress_to_target_pct = (mark - entry_price) / (target_price - entry_price) * 100
    progress_to_stop_pct   = (entry_price - mark) / (entry_price - stop_price) * 100

100% means the mark has reached the target/stop; negative means it has moved
the wrong way. Both are None if the corresponding target/stop wasn't set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .models import Position


@dataclass
class Valuation:
    position_id: str
    ticker: str
    asset_type: str
    asof_date: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    mark: Optional[float]
    iv: Optional[float]
    volume: Optional[int]
    open_interest: Optional[int]
    current_value: Optional[float]
    unrealized_pnl: Optional[float]
    unrealized_pnl_pct: Optional[float]
    dte: Optional[int]
    progress_to_target_pct: Optional[float]
    progress_to_stop_pct: Optional[float]
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _pct_progress(mark: Optional[float], entry: float, target: Optional[float], toward_stop: bool) -> Optional[float]:
    if target is None or mark is None:
        return None
    if toward_stop:
        denom = entry - target
        if denom == 0:
            return None
        return (entry - mark) / denom * 100.0
    denom = target - entry
    if denom == 0:
        return None
    return (mark - entry) / denom * 100.0


def compute_valuation(
    position: Position,
    market: dict,
    asof: date,
    greeks: Optional[dict] = None,
) -> Valuation:
    """
    market: dict with at least `mark`; options also carry bid/ask/last/iv/volume/open_interest
            (see market_data.get_option_market_data / get_underlying_quote).
    greeks: dict with delta/gamma/theta/vega (options only), or None for shares.
    """
    mark = market.get("mark")
    multiplier = position.multiplier

    current_value = mark * position.contracts * multiplier if mark is not None else None
    unrealized_pnl = (
        (mark - position.entry_price) * position.contracts * multiplier
        if mark is not None
        else None
    )
    unrealized_pnl_pct = (
        (mark - position.entry_price) / position.entry_price * 100.0
        if mark is not None and position.entry_price
        else None
    )

    dte = position.dte(asof)

    progress_to_target = _pct_progress(mark, position.entry_price, position.target_price, toward_stop=False)
    progress_to_stop = _pct_progress(mark, position.entry_price, position.stop_price, toward_stop=True)

    greeks = greeks or {}

    return Valuation(
        position_id=position.id,
        ticker=position.ticker,
        asset_type=position.asset_type,
        asof_date=asof.isoformat(),
        bid=market.get("bid"),
        ask=market.get("ask"),
        last=market.get("last"),
        mark=mark,
        iv=market.get("iv"),
        volume=market.get("volume"),
        open_interest=market.get("open_interest"),
        current_value=current_value,
        unrealized_pnl=unrealized_pnl,
        unrealized_pnl_pct=unrealized_pnl_pct,
        dte=dte,
        progress_to_target_pct=progress_to_target,
        progress_to_stop_pct=progress_to_stop,
        delta=greeks.get("delta"),
        gamma=greeks.get("gamma"),
        theta=greeks.get("theta"),
        vega=greeks.get("vega"),
    )
