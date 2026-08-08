"""New-strike / new-expiry detection: diffs today's pulled option chain
snapshot against the most recent prior snapshot for the same underlying
(see snapshot.py / db.snapshots).

Two distinct things are reported, matching what a strikes/expiries scan is
actually for:
  - new_expiries: every expiry that appeared today but wasn't in the prior
    snapshot, unfiltered -- this is how a fresh LEAP expiry shows up without
    scanning the whole chain.
  - new_strikes: newly-listed strikes (within an expiry that existed before,
    or a brand new one), filtered to strikes within a configurable band of a
    strike you actually hold on that underlying (default +/-30%), so you see
    "nearby" additions instead of noise from strikes nowhere near your book.

No prior snapshot (first run for a ticker) => nothing to diff against yet;
returns an empty result rather than treating everything as "new".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import DEFAULT_STRIKE_BAND


@dataclass
class ChainDiff:
    ticker: str
    new_expiries: list[str] = field(default_factory=list)
    new_strikes: dict[str, list[float]] = field(default_factory=dict)  # expiry -> sorted strikes

    def is_empty(self) -> bool:
        return not self.new_expiries and not self.new_strikes

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "new_expiries": self.new_expiries, "new_strikes": self.new_strikes}


def _strikes_in_chain(expiry_chain: dict) -> set[float]:
    strikes = set()
    for side in ("calls", "puts"):
        for row in expiry_chain.get(side, []):
            if row.get("strike") is not None:
                strikes.add(row["strike"])
    return strikes


def compute_diff(
    ticker: str,
    prior_snapshot_data: Optional[dict],
    today_snapshot: dict,
    held_strikes: list[float],
    strike_band: float = DEFAULT_STRIKE_BAND,
) -> ChainDiff:
    if prior_snapshot_data is None:
        return ChainDiff(ticker=ticker)

    prior_expiries_map = prior_snapshot_data.get("expiries", {})
    today_expiries_map = today_snapshot.get("expiries", {})

    new_expiries = sorted(set(today_expiries_map) - set(prior_expiries_map))

    held = [h for h in held_strikes if h]
    new_strikes: dict[str, list[float]] = {}
    for expiry, chain in today_expiries_map.items():
        today_strikes = _strikes_in_chain(chain)
        prior_strikes = _strikes_in_chain(prior_expiries_map.get(expiry, {}))
        added = today_strikes - prior_strikes
        if not added:
            continue
        in_band = sorted(s for s in added if any(abs(s - h) / h <= strike_band for h in held))
        if in_band:
            new_strikes[expiry] = in_band

    return ChainDiff(ticker=ticker, new_expiries=new_expiries, new_strikes=new_strikes)
