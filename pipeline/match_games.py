"""Match Kalshi ladders to CFBD games.

Kalshi uses display names ("Miami (FL)", "Ohio St.") while CFBD uses its own
school names ("Miami", "Ohio State").  Most of the gap is the mechanical
"St." -> "State" rewrite plus punctuation; the genuinely ambiguous cases live
in team_aliases.json.

Nothing is dropped silently: every ladder that fails to match is returned in a
report so the alias file can be corrected rather than the game quietly vanishing
from the slate.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass
from typing import Optional

from .margin_model import Ladder

_ALIASES: Optional[dict] = None

_SUFFIX_FIXES = [
    (re.compile(r"\bst\.?\b", re.I), "state"),
    (re.compile(r"\bu\.?\b(?!\w)", re.I), ""),
    (re.compile(r"\buniv(ersity)?\b", re.I), ""),
    (re.compile(r"\bn\.?c\.?\b", re.I), "north carolina"),
]


def aliases() -> dict:
    global _ALIASES
    if _ALIASES is None:
        path = os.path.join(os.path.dirname(__file__), "team_aliases.json")
        with open(path, encoding="utf-8") as fh:
            _ALIASES = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    return _ALIASES


def normalize(name: str) -> str:
    """Collapse a school name to a comparable key.

    Parenthetical qualifiers are preserved (Miami (FL) and Miami (OH) are
    different schools), so those resolve through the alias file instead.
    """
    if not name:
        return ""
    s = aliases().get(name.strip(), name).strip()
    for pattern, repl in _SUFFIX_FIXES:
        s = pattern.sub(repl, s)
    s = s.replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _date(value: Optional[str]) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


@dataclass
class Match:
    ladder: Ladder
    game: Optional[dict]
    line: Optional[dict]
    flipped: bool = False

    @property
    def home_favored_by(self) -> Optional[float]:
        """Vegas line oriented to the ladder's home team."""
        if not self.line or self.line.get("home_favored_by") is None:
            return None
        value = float(self.line["home_favored_by"])
        return -value if self.flipped else value

    @property
    def home_margin(self) -> Optional[float]:
        """Final margin oriented to the ladder's home team."""
        if not self.game or self.game.get("home_margin") is None:
            return None
        value = float(self.game["home_margin"])
        return -value if self.flipped else value


def _index(records: list[dict]) -> dict:
    """Key CFBD records by the unordered normalized team pair."""
    out: dict[frozenset, list[dict]] = {}
    for r in records:
        home, away = normalize(r.get("home_team", "")), normalize(r.get("away_team", ""))
        if not home or not away:
            continue
        out.setdefault(frozenset((home, away)), []).append(r)
    return out


def _lookup(index: dict, ladder: Ladder) -> tuple[Optional[dict], bool]:
    home, away = normalize(ladder.home_team), normalize(ladder.away_team)
    candidates = index.get(frozenset((home, away)))
    if not candidates:
        return None, False

    target = _date(ladder.close_time)
    best, best_gap = None, None
    for c in candidates:
        gap = 0
        cd = _date(c.get("start_date"))
        if target and cd:
            gap = abs((cd - target).days)
        if best_gap is None or gap < best_gap:
            best, best_gap = c, gap

    # Kickoff times cross UTC midnight, so allow a day either way but no more.
    if best_gap is not None and best_gap > 2:
        return None, False
    flipped = normalize(best.get("home_team", "")) != home
    return best, flipped


def match_all(ladders: list[Ladder], games: list[dict],
              lines: list[dict]) -> tuple[list[Match], list[str]]:
    """Join ladders to CFBD games and lines.

    Returns (matches, unmatched_titles).  A ladder with no CFBD counterpart
    still yields a Match with `game`/`line` unset, so the Kalshi read survives
    and the manual spread box remains usable for it.
    """
    game_index, line_index = _index(games), _index(lines)
    matches, unmatched = [], []

    for ladder in ladders:
        game, g_flip = _lookup(game_index, ladder)
        line, l_flip = _lookup(line_index, ladder)
        if game is None and line is None:
            unmatched.append(ladder.title)
        matches.append(Match(ladder=ladder, game=game, line=line,
                             flipped=g_flip if game is not None else l_flip))
    return matches, unmatched
