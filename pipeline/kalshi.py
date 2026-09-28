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

    Every rung is a claim about the same number -- the home margin -- so each
    settlement is one inequality and the ladder is their intersection.

    A home rung `t` asks {margin > t}, so its yes is a lower bound and its no an
    upper one.  An away rung `t` asks {margin < -t}, because "away wins by over
    t" is the same event read from the other end, so it is the other way round:
    its YES bounds the margin from above.  That inversion is easy to get
    backwards and it does not blur the answer, it turns it inside out -- the
    away branches used to be swapped and the brackets came back with a median
    width of MINUS twenty points.  Hence one condition, two bounds, and a test
    per combination.
    """
    lower, upper = -float("inf"), float("inf")
    for m in markets:
        result = (m.get("result") or "").lower()
        strike = m.get("floor_strike")
        if result not in ("yes", "no") or strike is None:
            continue
        suffix = m["ticker"].rsplit("-", 1)[-1]
        abbrev = "".join(c for c in suffix if not c.isdigit())
        home = abbrev == home_abbrev
        signed = float(strike) if home else -float(strike)
        if home == (result == "yes"):
            lower = max(lower, signed)       # home yes, away no: margin above
        else:
            upper = min(upper, signed)       # home no, away yes: margin below
    if lower == -float("inf") and upper == float("inf"):
        return None
    if upper <= lower:
        # No margin satisfies this, so the ladder is not saying what it appears
        # to: a mis-read side, a settlement correction, or two events sharing a
        # ticker. An impossible bracket is worse than no bracket -- it grades
        # games wrong with full confidence -- so it is not returned as one.
        return None
    return lower, upper


def settled_by_event(markets: list[dict]) -> dict[str, list[dict]]:
    """Group settled markets by their event ticker."""
    out: dict[str, list[dict]] = {}
    for m in markets:
        ticker = m.get("event_ticker")
        if ticker:
            out.setdefault(ticker, []).append(m)
    return out


def home_abbrev_from_event(event_ticker: str, markets: list[dict]) -> Optional[str]:
    """Which of a settled event's abbreviations is the home side.

    Kalshi writes the event ticker as ``<series>-<date><away><home>``, so the
    home abbreviation is the one the ticker ends with.  It is matched against
    the abbreviations the markets themselves carry rather than split by
    position, because the codes are not fixed width; where both match -- an
    away code that is also a tail of the home code, SC at USC -- the longer one
    is the home side, since the shorter can only match by being a suffix of it.

    Returns "" when the ladder quotes one team and that team is the away side:
    no rung belongs to the home team, which is a real answer rather than a
    failure.  None means the ticker and the rungs disagree, and nothing should
    be graded from that.
    """
    seen = set()
    for m in markets:
        suffix = (m.get("ticker") or "").rsplit("-", 1)[-1]
        abbrev = "".join(c for c in suffix if not c.isdigit())
        if abbrev:
            seen.add(abbrev)
    if not seen:
        return None
    tail = event_ticker.rsplit("-", 1)[-1]
    matches = [a for a in seen if tail.endswith(a) and tail != a]
    if matches:
        return max(matches, key=len)
    return "" if len(seen) == 1 else None
