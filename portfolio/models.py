"""Position types for the options + equity portfolio.

Both asset types live in one book (positions.json). Options carry
per-share entry_price/target_price/stop_price -- multiply by
OPTION_MULTIPLIER (100) and `contracts` to get dollar amounts.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Any, Optional

from .config import OPTION_MULTIPLIER

VALID_ASSET_TYPES = {"option", "shares"}
VALID_OPTION_TYPES = {"call", "put"}


class PositionError(ValueError):
    """Raised when a position record in positions.json is invalid."""


@dataclass
class Position:
    id: str
    asset_type: str  # "option" | "shares"
    ticker: str
    entry_price: float
    contracts: float  # number of option contracts, or number of shares
    entry_date: str
    option_type: Optional[str] = None  # "call" | "put" (options only)
    strike: Optional[float] = None  # options only
    expiry: Optional[str] = None  # YYYY-MM-DD, options only
    target_price: Optional[float] = None  # per share
    stop_price: Optional[float] = None  # per share

    @property
    def is_option(self) -> bool:
        return self.asset_type == "option"

    @property
    def multiplier(self) -> int:
        """Shares represented per unit of `contracts`."""
        return OPTION_MULTIPLIER if self.is_option else 1

    @property
    def expiry_date(self) -> Optional[date]:
        if not self.expiry:
            return None
        return datetime.strptime(self.expiry, "%Y-%m-%d").date()

    def dte(self, asof: date) -> Optional[int]:
        """Days to expiry as of a given date. None for shares."""
        if not self.is_option:
            return None
        return (self.expiry_date - asof).days

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Position":
        data = dict(raw)  # don't mutate caller's dict

        asset_type = data.get("asset_type")
        if asset_type not in VALID_ASSET_TYPES:
            raise PositionError(
                f"asset_type must be one of {VALID_ASSET_TYPES}, got {asset_type!r}"
            )

        ticker = data.get("ticker")
        if not ticker:
            raise PositionError("ticker is required")
        ticker = ticker.upper()

        required = ["entry_price", "contracts", "entry_date"]
        if asset_type == "option":
            required += ["option_type", "strike", "expiry"]
        missing = [f for f in required if data.get(f) in (None, "")]
        if missing:
            raise PositionError(f"{ticker}: missing required field(s) {missing}")

        option_type = data.get("option_type")
        if asset_type == "option":
            option_type = option_type.lower()
            if option_type not in VALID_OPTION_TYPES:
                raise PositionError(
                    f"{ticker}: option_type must be one of {VALID_OPTION_TYPES}, got {option_type!r}"
                )

        strike = float(data["strike"]) if data.get("strike") is not None else None
        expiry = data.get("expiry")

        pos_id = data.get("id")
        if not pos_id:
            pos_id = cls._auto_id(
                ticker=ticker,
                asset_type=asset_type,
                option_type=option_type,
                strike=strike,
                expiry=expiry,
                entry_date=data.get("entry_date"),
            )

        return cls(
            id=pos_id,
            asset_type=asset_type,
            ticker=ticker,
            entry_price=float(data["entry_price"]),
            contracts=float(data["contracts"]),
            entry_date=data["entry_date"],
            option_type=option_type,
            strike=strike,
            expiry=expiry,
            target_price=(
                float(data["target_price"]) if data.get("target_price") is not None else None
            ),
            stop_price=(
                float(data["stop_price"]) if data.get("stop_price") is not None else None
            ),
        )

    @staticmethod
    def _auto_id(
        ticker: str,
        asset_type: str,
        option_type: Optional[str],
        strike: Optional[float],
        expiry: Optional[str],
        entry_date: Optional[str],
    ) -> str:
        if asset_type == "option":
            strike_str = f"{strike:g}"
            return f"{ticker}-{option_type.upper()}-{strike_str}-{expiry}"
        return f"{ticker}-SHARES-{entry_date}"


def load_positions(raw_records: list[dict]) -> list[Position]:
    positions = [Position.from_dict(r) for r in raw_records]
    ids = [p.id for p in positions]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise PositionError(f"Duplicate position id(s): {dupes}")
    return positions


def load_positions_file(path: str) -> list[Position]:
    import json

    with open(path, "r") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise PositionError("positions.json must contain a JSON array")
    return load_positions(raw)
