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


def select(ladders: list[Ladder], top_n: int = None) -> list[Ladder]:
    """The week's slate. Locked at the decision snapshot by the caller so a
    game cannot drop out of the record retroactively."""
    return rank(ladders)[: (top_n or config.TOP_N)]
