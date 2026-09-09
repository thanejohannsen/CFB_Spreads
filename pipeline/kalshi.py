"""Fetch college football spread ladders from Kalshi.

The public market-data endpoints need no authentication, and the entire slate
comes back in a single request with markets nested -- so a full refresh costs
about two API calls regardless of how often it runs.
"""

from __future__ import annotations

from typing import Iterator, Optional

from . import config
from .http import get_json
from .margin_model import Ladder, parse_ladder


def _paginate(path: str, params: dict, key: str, page_limit: int = 20) -> Iterator[dict]:
    cursor: Optional[str] = None
    for _ in range(page_limit):
        payload = get_json(f"{config.KALSHI_BASE}{path}", {**params, "cursor": cursor})
        yield from payload.get(key, [])
        cursor = payload.get("cursor")
        if not cursor:
            return


def fetch_events(status: str = "open", series: str = None) -> list[dict]:
    """Every event in the series, with its markets nested."""
    return list(_paginate(
        "/events",
        {"series_ticker": series or config.KALSHI_SERIES,
         "status": status, "limit": 200, "with_nested_markets": "true"},
        "events",
    ))


def fetch_ladders(status: str = "open") -> tuple[list[Ladder], list[str]]:
    """Parse every event into a Ladder.

    Returns (ladders, skipped) so nothing is dropped silently -- a game the
    parser cannot read is reported rather than quietly missing from the slate.
    """
    ladders, skipped = [], []
    for event in fetch_events(status=status):
        ladder = parse_ladder(event)
        if ladder is None:
            skipped.append(event.get("event_ticker", "<unknown>"))
        else:
            ladders.append(ladder)
    return ladders, skipped


def fetch_settled_markets(series: str = None) -> list[dict]:
    """Settled markets retain `result` per strike, which brackets the final
    margin and provides an independent cross-check when grading."""
    return list(_paginate(
        "/markets",
        {"series_ticker": series or config.KALSHI_SERIES, "status": "settled", "limit": 1000},
        "markets",
    ))


def margin_from_settled(markets: list[dict], home_abbrev: str) -> Optional[tuple[float, float]]:
    """Bracket the final home margin from a settled ladder's yes/no results.

    The highest home strike resolving "yes" is a lower bound on the margin; the
    lowest resolving "no" is an upper bound.
    """
    lower, upper = -float("inf"), float("inf")
    for m in markets:
        result = (m.get("result") or "").lower()
        strike = m.get("floor_strike")
        if result not in ("yes", "no") or strike is None:
            continue
        suffix = m["ticker"].rsplit("-", 1)[-1]
        abbrev = "".join(c for c in suffix if not c.isdigit())
        signed = float(strike) if abbrev == home_abbrev else -float(strike)
        if (abbrev == home_abbrev) == (result == "yes"):
            lower = max(lower, signed) if abbrev == home_abbrev else lower
            upper = min(upper, signed) if abbrev != home_abbrev else upper
        else:
            upper = min(upper, signed) if abbrev == home_abbrev else upper
            lower = max(lower, signed) if abbrev != home_abbrev else lower
    if lower == -float("inf") and upper == float("inf"):
        return None
    return lower, upper
