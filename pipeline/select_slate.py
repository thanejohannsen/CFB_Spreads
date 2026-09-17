"""Pick the week's slate: the TOP_N most popular Kalshi games.

Popularity and tradeability are the same signal.  Ranking by total open
interest across a game's ladder selects, without any hand-curated list of
"important" teams, exactly the games whose markets are liquid enough to read --
and drops the FCS filler whose quotes are market-maker placeholders.
"""

from __future__ import annotations

from . import config
from .margin_model import Ladder


def rank(ladders: list[Ladder]) -> list[Ladder]:
    """Most popular first, by total open interest across the ladder."""
    return sorted(ladders, key=lambda l: -l.total_open_interest)


def select(ladders: list[Ladder], top_n: int = None,
           pinned: set = None) -> list[Ladder]:
    """The week's slate: the TOP_N most popular, plus anything already pinned.

    Open interest keeps moving after the decision deadline, so a game locked
    into the record on Wednesday can be pushed out of the top N by Saturday.
    When that happened the board stopped showing it while the record went on
    grading it -- and, worse, its T-1h lock could never be refreshed again,
    because a game absent from the payload is a game history never sees. The
    caller pins every game this week has already locked, so the slate can only
    grow after the deadline, never shed a game the record is committed to.
    """
    ranked = rank(ladders)
    chosen = ranked[: (top_n or config.TOP_N)]
    if not pinned:
        return chosen
    have = {l.event_ticker for l in chosen}
    return chosen + [l for l in ranked
                     if l.event_ticker in pinned and l.event_ticker not in have]
