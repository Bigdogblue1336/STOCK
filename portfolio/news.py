"""Daily Claude news analysis, run once per held underlying.

For each ticker in the book: pull recent headlines (yfinance .news) within a
configurable window, send them to Claude, and get back a factual summary,
sentiment, key drivers, and a flag for anything that specifically touches a
held position. This is explicitly NOT a trade signal -- the prompt forbids
buy/sell/hold language and price predictions, and nothing downstream should
treat position_flag as anything but "read this one."

Each ticker's analysis is cached per calendar day in SQLite (see db.py
`news_analysis`, unique on ticker+asof_date) so re-running the same day never
re-bills the API -- the cache is checked before any API call is made.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from . import db as db_module
from .config import DEFAULT_NEWS_MAX_TOKENS, DEFAULT_NEWS_MODEL, DEFAULT_NEWS_WINDOW_DAYS
from .market_data import MarketDataError, get_news
from .models import Position

logger = logging.getLogger(__name__)

VALID_SENTIMENTS = {"positive", "neutral", "negative"}

NEWS_SYSTEM_PROMPT = """You are a market news summarizer for a personal options/equity portfolio \
monitoring tool. You read recent headlines for one ticker and report facts only.

Strict rules:
- Never recommend buying, selling, holding, rolling, or any other trading action.
- Never predict future price moves.
- Only report what the headlines say happened and the drivers they cite.
- If headlines are thin, stale, or contradictory, say so plainly rather than guessing.
- Respond with JSON only. No prose before or after the JSON object."""


class NewsAnalysisError(RuntimeError):
    pass


@dataclass
class NewsAnalysis:
    ticker: str
    asof_date: str
    summary: str
    sentiment: str
    key_drivers: list[str]
    position_flag: bool
    position_flag_detail: str
    headlines: list[dict] = field(default_factory=list)
    model: Optional[str] = None
    from_cache: bool = False

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _describe_position(position: Position, asof: date) -> str:
    if position.is_option:
        dte = position.dte(asof)
        return (
            f"{position.option_type.upper()} {position.strike:g} strike, expires {position.expiry} "
            f"({dte} DTE), entry ${position.entry_price:.2f}/share"
            + (f", target ${position.target_price:.2f}" if position.target_price is not None else "")
            + (f", stop ${position.stop_price:.2f}" if position.stop_price is not None else "")
        )
    return (
        f"{position.contracts:g} shares, entry ${position.entry_price:.2f}"
        + (f", target ${position.target_price:.2f}" if position.target_price is not None else "")
        + (f", stop ${position.stop_price:.2f}" if position.stop_price is not None else "")
    )


def build_user_prompt(
    ticker: str, headlines: list[dict], positions: list[Position], asof: date, window_days: int
) -> str:
    if headlines:
        headline_lines = "\n".join(
            f"- [{h['published_at'] or 'unknown time'}] {h['title']} ({h['publisher'] or 'unknown source'})"
            for h in headlines
        )
    else:
        headline_lines = "(no recent headlines found)"

    position_lines = "\n".join(f"- {_describe_position(p, asof)}" for p in positions) or "(none)"

    return f"""Ticker: {ticker}

Recent headlines (last {window_days} days):
{headline_lines}

Held position(s) on this ticker, for relevance judgment only -- not for advice:
{position_lines}

Respond with JSON only, matching exactly this schema:
{{
  "summary": "2-4 sentence factual summary of what actually happened",
  "sentiment": "positive" | "neutral" | "negative",
  "key_drivers": ["short driver 1", "short driver 2"],
  "position_flag": true or false,
  "position_flag_detail": "if position_flag is true, one factual sentence on what specifically \
about this news touches the held position(s) above; empty string otherwise"
}}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise NewsAnalysisError(f"Could not parse JSON from Claude response: {text[:200]!r}")


def build_client():
    """Returns an anthropic.Anthropic client, or None (with a logged warning) if no API key."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY not set; skipping news analysis for this run.")
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed; skipping news analysis. `pip install anthropic`.")
        return None
    return anthropic.Anthropic()


def call_claude_news_analysis(
    client,
    ticker: str,
    headlines: list[dict],
    positions: list[Position],
    asof: date,
    window_days: int = DEFAULT_NEWS_WINDOW_DAYS,
    model: str = DEFAULT_NEWS_MODEL,
    max_tokens: int = DEFAULT_NEWS_MAX_TOKENS,
) -> dict:
    prompt = build_user_prompt(ticker, headlines, positions, asof, window_days)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=NEWS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    except Exception as exc:
        raise NewsAnalysisError(f"Claude API call failed for {ticker}: {exc}") from exc

    parsed = _extract_json(text)

    summary = str(parsed.get("summary") or "").strip()
    sentiment = str(parsed.get("sentiment") or "neutral").strip().lower()
    if sentiment not in VALID_SENTIMENTS:
        logger.warning("%s: unexpected sentiment %r from Claude, defaulting to neutral", ticker, sentiment)
        sentiment = "neutral"
    key_drivers = parsed.get("key_drivers") or []
    if not isinstance(key_drivers, list):
        key_drivers = [str(key_drivers)]
    position_flag = bool(parsed.get("position_flag", False))
    position_flag_detail = str(parsed.get("position_flag_detail") or "").strip()

    return {
        "summary": summary,
        "sentiment": sentiment,
        "key_drivers": [str(d) for d in key_drivers],
        "position_flag": position_flag,
        "position_flag_detail": position_flag_detail,
    }


def get_or_create_news_analysis(
    conn: sqlite3.Connection,
    client,
    ticker: str,
    ticker_positions: list[Position],
    asof: date,
    run_timestamp: str,
    window_days: int = DEFAULT_NEWS_WINDOW_DAYS,
    model: str = DEFAULT_NEWS_MODEL,
    max_tokens: int = DEFAULT_NEWS_MAX_TOKENS,
) -> NewsAnalysis:
    """Cache-first: returns today's stored analysis for `ticker` if present, else
    calls Claude once and stores the result. Never calls the API twice for the
    same ticker+asof_date."""
    cached = db_module.get_news_analysis(conn, ticker, asof.isoformat())
    if cached:
        return NewsAnalysis(**cached, from_cache=True)

    try:
        headlines = get_news(ticker, window_days=window_days)
    except MarketDataError as exc:
        logger.warning("news: could not fetch headlines for %s: %s", ticker, exc)
        headlines = []

    parsed = call_claude_news_analysis(
        client, ticker, headlines, ticker_positions, asof, window_days, model, max_tokens
    )

    result = NewsAnalysis(
        ticker=ticker,
        asof_date=asof.isoformat(),
        summary=parsed["summary"],
        sentiment=parsed["sentiment"],
        key_drivers=parsed["key_drivers"],
        position_flag=parsed["position_flag"],
        position_flag_detail=parsed["position_flag_detail"],
        headlines=headlines,
        model=model,
        from_cache=False,
    )
    db_module.save_news_analysis(conn, run_timestamp, result.to_dict())
    return result
