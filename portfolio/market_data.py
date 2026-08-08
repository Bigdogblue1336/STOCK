"""Market data pulls via yfinance.

Kept as a thin wrapper so later layers (and tests) can mock this module
without touching yfinance directly.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - exercised only when dependency missing
    yf = None


class MarketDataError(RuntimeError):
    pass


def _require_yfinance() -> None:
    if yf is None:
        raise MarketDataError(
            "yfinance is not installed. Run `pip install -r requirements.txt`."
        )


def _clean(value: Any) -> Optional[float]:
    """Normalize NaN/None/missing numeric fields from yfinance to None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def mark_from_quote(bid: Optional[float], ask: Optional[float], last: Optional[float]) -> Optional[float]:
    """mid = (bid+ask)/2 when both present and positive, else last."""
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return last


def get_underlying_quote(ticker: str) -> dict:
    """Current spot quote for the underlying (used as mark for `shares` and as S for BS)."""
    _require_yfinance()
    t = yf.Ticker(ticker)

    last = bid = ask = None
    try:
        fi = t.fast_info
        last = _clean(getattr(fi, "last_price", None))
        bid = _clean(getattr(fi, "bid", None))
        ask = _clean(getattr(fi, "ask", None))
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning("fast_info lookup failed for %s: %s", ticker, exc)

    if last is None:
        try:
            info = t.info or {}
            last = _clean(info.get("regularMarketPrice") or info.get("currentPrice"))
            bid = bid if bid is not None else _clean(info.get("bid"))
            ask = ask if ask is not None else _clean(info.get("ask"))
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning(".info lookup failed for %s: %s", ticker, exc)

    if last is None:
        try:
            hist = t.history(period="1d")
            if not hist.empty:
                last = _clean(hist["Close"].iloc[-1])
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("history lookup failed for %s: %s", ticker, exc)

    if last is None:
        raise MarketDataError(f"Could not fetch a spot price for {ticker}")

    mark = mark_from_quote(bid, ask, last)
    return {"ticker": ticker, "bid": bid, "ask": ask, "last": last, "mark": mark}


def list_expiries(ticker: str) -> list[str]:
    _require_yfinance()
    try:
        return list(yf.Ticker(ticker).options)
    except Exception as exc:  # pragma: no cover - network dependent
        raise MarketDataError(f"Could not list option expiries for {ticker}: {exc}") from exc


def _row_to_dict(row) -> dict:
    return {
        "contractSymbol": row.get("contractSymbol"),
        "strike": _clean(row.get("strike")),
        "bid": _clean(row.get("bid")),
        "ask": _clean(row.get("ask")),
        "last": _clean(row.get("lastPrice")),
        "iv": _clean(row.get("impliedVolatility")),
        "volume": int(row["volume"]) if _clean(row.get("volume")) is not None else None,
        "open_interest": (
            int(row["openInterest"]) if _clean(row.get("openInterest")) is not None else None
        ),
        "in_the_money": bool(row.get("inTheMoney")) if row.get("inTheMoney") is not None else None,
    }


def get_expiry_chain(ticker: str, expiry: str) -> dict:
    """Full calls+puts chain for one expiry, as JSON-serializable dicts."""
    _require_yfinance()
    try:
        chain = yf.Ticker(ticker).option_chain(expiry)
    except Exception as exc:  # pragma: no cover - network dependent
        raise MarketDataError(f"Could not fetch option chain for {ticker} {expiry}: {exc}") from exc

    calls = [_row_to_dict(r) for r in chain.calls.to_dict("records")]
    puts = [_row_to_dict(r) for r in chain.puts.to_dict("records")]
    return {"calls": calls, "puts": puts}


def get_full_chain_snapshot(ticker: str, asof: date, expiries: Optional[list[str]] = None) -> dict:
    """Pull the full option chain for a ticker across all (or given) expiries.

    Used both for the option-market-data lookups needed by valuation, and to
    persist a dated snapshot for later new-strike / new-expiry diffing.
    """
    _require_yfinance()
    quote = get_underlying_quote(ticker)
    all_expiries = list_expiries(ticker)
    target_expiries = expiries if expiries is not None else all_expiries

    expiries_data = {}
    for exp in target_expiries:
        if exp not in all_expiries:
            logger.warning("%s: expiry %s not currently listed by yfinance, skipping", ticker, exp)
            continue
        expiries_data[exp] = get_expiry_chain(ticker, exp)

    return {
        "ticker": ticker,
        "asof": asof.isoformat(),
        "pulled_at": datetime.utcnow().isoformat() + "Z",
        "spot": quote,
        "available_expiries": all_expiries,
        "expiries": expiries_data,
    }


def find_contract(
    chain_snapshot: dict, expiry: str, option_type: str, strike: float
) -> Optional[dict]:
    """Locate one contract's market data inside a get_full_chain_snapshot() result."""
    exp_data = chain_snapshot.get("expiries", {}).get(expiry)
    if not exp_data:
        return None
    side = "calls" if option_type.lower() == "call" else "puts"
    for row in exp_data.get(side, []):
        if row.get("strike") is not None and math.isclose(row["strike"], strike, abs_tol=1e-6):
            return row
    return None


def get_sector_fallback(ticker: str) -> Optional[str]:
    """Best-effort sector lookup via yfinance, used when a ticker isn't in the sector config map."""
    _require_yfinance()
    try:
        info = yf.Ticker(ticker).info or {}
        return info.get("sector")
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning("sector lookup failed for %s: %s", ticker, exc)
        return None


def get_option_market_data(
    chain_snapshot: dict, expiry: str, option_type: str, strike: float
) -> dict:
    """Build the market-data dict (mark, iv, volume, oi, ...) for one held option."""
    row = find_contract(chain_snapshot, expiry, option_type, strike)
    if row is None:
        raise MarketDataError(
            f"No contract found for {chain_snapshot.get('ticker')} "
            f"{expiry} {option_type} {strike} in pulled chain"
        )
    mark = mark_from_quote(row.get("bid"), row.get("ask"), row.get("last"))
    return {**row, "mark": mark}
