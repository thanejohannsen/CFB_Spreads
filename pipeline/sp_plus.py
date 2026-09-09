"""Turn SP+ ratings into a projected spread.

SP+ is Bill Connelly's tempo- and opponent-adjusted efficiency rating, published
at ESPN and carried by the CollegeFootballData API.  A rating is how many points
better than average a team is **on a neutral field**, so a matchup projection is
just the rating difference plus home-field advantage.

This is deliberately a lens and not part of the master pick.  SP+ grades around
52-54% against the closing spread, which sits on top of the 52.4% break-even a
-110 line demands -- informative to watch, not sharp enough to move a number set
by markets carrying hundreds of thousands of contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import config
from .match_games import normalize


@dataclass
class SPProjection:
    home_favored_by: float
    home_rating: float
    away_rating: float
    home_field: float
    neutral_site: bool

    def describe(self) -> str:
        site = "neutral site" if self.neutral_site else f"+{self.home_field:.1f} home field"
        return (f"SP+ {self.home_rating:+.1f} vs {self.away_rating:+.1f}, {site}")


def index_ratings(ratings: dict[str, float]) -> dict[str, float]:
    """Key SP+ ratings by the same normalised team name the matcher uses."""
    return {normalize(team): value for team, value in ratings.items() if team}


def project(ratings_index: dict[str, float], home_team: str, away_team: str,
            neutral_site: bool = False) -> Optional[SPProjection]:
    """Projected spread from the home team's perspective.

    Returns None when either team has no rating -- FCS opponents mostly do not,
    and inventing a number for them would be worse than showing nothing.
    """
    home = ratings_index.get(normalize(home_team))
    away = ratings_index.get(normalize(away_team))
    if home is None or away is None:
        return None

    hfa = 0.0 if neutral_site else config.HOME_FIELD_ADVANTAGE
    return SPProjection(
        home_favored_by=(home - away) + hfa,
        home_rating=home,
        away_rating=away,
        home_field=hfa,
        neutral_site=neutral_site,
    )
