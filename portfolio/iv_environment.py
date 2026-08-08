"""Per-position IV environment: current IV, IV rank/percentile built from our
own stored daily chain snapshots (portfolio.db `snapshots` table -- see
snapshot.py), rich/cheap flags, and a near-expiry visibility flag.

This surfaces facts only -- it never recommends rolling or any other action.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from . import db as db_module
from .config import (
    DEFAULT_IV_CHEAP_THRESHOLD,
    DEFAULT_IV_LOOKBACK_DAYS,
    DEFAULT_IV_MIN_HISTORY_DAYS,
    DEFAULT_IV_RICH_THRESHOLD,
    DEFAULT_NEAR_EXPIRY_DTE,
)
from .market_data import find_contract
from .models import Position

STATUS_OK = "ok"
STATUS_BUILDING_HISTORY = "building_history"


@dataclass
class IVEnvironment:
    position_id: str
    ticker: str
    current_iv: Optional[float]
    iv_rank: Optional[float]
    iv_percentile: Optional[float]
    sample_size: int
    status: str
    rich_cheap: Optional[str]  # 'rich' | 'cheap' | 'normal' | None
    dte: Optional[int]
    near_expiry: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _iv_history_for_contract(
    conn: sqlite3.Connection, position: Position, asof: date, lookback_days: int
) -> list[float]:
    start = (asof - timedelta(days=lookback_days)).isoformat()
    end = asof.isoformat()
    snapshots = db_module.get_snapshots_in_range(conn, position.ticker, start, end)

    values = []
    for snap in snapshots:
        contract = find_contract(snap["data"], position.expiry, position.option_type, position.strike)
        if contract and contract.get("iv") is not None:
            values.append(contract["iv"])
    return values


def compute_iv_environment(
    conn: sqlite3.Connection,
    position: Position,
    current_iv: Optional[float],
    asof: date,
    lookback_days: int = DEFAULT_IV_LOOKBACK_DAYS,
    min_history_days: int = DEFAULT_IV_MIN_HISTORY_DAYS,
    rich_threshold: float = DEFAULT_IV_RICH_THRESHOLD,
    cheap_threshold: float = DEFAULT_IV_CHEAP_THRESHOLD,
    near_expiry_dte: int = DEFAULT_NEAR_EXPIRY_DTE,
) -> IVEnvironment:
    dte = position.dte(asof)
    near_expiry = dte is not None and dte <= near_expiry_dte

    if current_iv is None:
        return IVEnvironment(
            position_id=position.id,
            ticker=position.ticker,
            current_iv=None,
            iv_rank=None,
            iv_percentile=None,
            sample_size=0,
            status=STATUS_BUILDING_HISTORY,
            rich_cheap=None,
            dte=dte,
            near_expiry=near_expiry,
        )

    history = _iv_history_for_contract(conn, position, asof, lookback_days)
    sample_size = len(history)

    if sample_size < min_history_days:
        return IVEnvironment(
            position_id=position.id,
            ticker=position.ticker,
            current_iv=current_iv,
            iv_rank=None,
            iv_percentile=None,
            sample_size=sample_size,
            status=STATUS_BUILDING_HISTORY,
            rich_cheap=None,
            dte=dte,
            near_expiry=near_expiry,
        )

    iv_min, iv_max = min(history), max(history)
    iv_rank = ((current_iv - iv_min) / (iv_max - iv_min) * 100.0) if iv_max > iv_min else 50.0
    iv_percentile = sum(1 for v in history if v <= current_iv) / sample_size * 100.0

    if iv_rank > rich_threshold:
        rich_cheap = "rich"
    elif iv_rank < cheap_threshold:
        rich_cheap = "cheap"
    else:
        rich_cheap = "normal"

    return IVEnvironment(
        position_id=position.id,
        ticker=position.ticker,
        current_iv=current_iv,
        iv_rank=iv_rank,
        iv_percentile=iv_percentile,
        sample_size=sample_size,
        status=STATUS_OK,
        rich_cheap=rich_cheap,
        dte=dte,
        near_expiry=near_expiry,
    )
