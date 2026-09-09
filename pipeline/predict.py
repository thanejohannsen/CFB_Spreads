"""Turn a market read plus a line into a pick, or an explicit refusal.

INVARIANT: docs/app.js reimplements `evaluate` so the page can recompute
instantly when a spread is typed by hand.  The two must stay in step -- the
rules are deliberately few and simple so that stays cheap.  pipeline/tests/
pins the behaviour of this side.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from . import config
from .liquidity import Blend, Quality
from .margin_model import MarketRead, cover_probabilities


@dataclass
class Pick:
    side: Optional[str]              # "home" | "away" | None when no play
    team: Optional[str]
    line: float                      # home-perspective, + means home gives points
    edge: float                      # blended implied margin minus the line
    p_cover: Optional[float]
    p_push: float
    confidence: str                  # "no-signal" | "no-play" | "lean" | "solid" | "strong"
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(read: MarketRead, quality: Quality, blend: Blend,
             line: Optional[float], home_team: str, away_team: str) -> Pick:
    """Decide whether the market disagrees with `line` by enough to act on."""

    # 1. A ladder that cannot be read produces no pick at all.  Shrinkage must
    #    never launder the Vegas line as a Kalshi prediction.
    if quality.tier == "D" or read.rejected:
        why = "; ".join(quality.reasons) or (read.rejected or "unreadable ladder")
        return Pick(None, None, line or 0.0, 0.0, None, 0.0, "no-signal",
                    f"Insufficient market: {why}.")

    if line is None:
        return Pick(None, None, 0.0, 0.0, None, 0.0, "no-play",
                    "No Vegas line available - enter one to grade this game.")

    probs = cover_probabilities(read.curve_mid, line)
    edge = blend.margin - line

    # 2. If the line sits inside the market's own uncertainty band, the
    #    apparent edge is smaller than the noise it was measured against.
    if read.margin_low <= line <= read.margin_high:
        return Pick(None, None, line, edge, probs.best, probs.push, "no-play",
                    f"Line sits inside the market's own {read.band:.1f}pt uncertainty "
                    f"band ({read.margin_low:+.1f} to {read.margin_high:+.1f}); "
                    "no edge worth acting on.")

    # 3. The curve must actually be pinned down where it is being read.
    distance = read.strike_distance(line)
    unpinned = distance > config.MAX_STRIKE_DISTANCE

    side = "home" if edge > 0 else "away"
    team = home_team if side == "home" else away_team
    p_cover = probs.home if side == "home" else probs.away

    magnitude = abs(edge)
    if unpinned or magnitude < 1.0:
        confidence = "lean"
    elif magnitude < 2.5:
        confidence = "solid"
    else:
        confidence = "strong"
    if quality.tier == "C":
        confidence = "lean"

    spread_text = _format_line(line, side)
    reason = (f"Market implies {_margin_text(blend.margin, home_team, away_team)}; "
              f"line is {_margin_text(line, home_team, away_team)}. "
              f"At that number the market gives {team} a {p_cover * 100:.0f}% "
              f"chance to cover. Edge {magnitude:.1f} pts.")
    if unpinned:
        reason += f" Nearest constraining strike is {distance:.1f} pts away, so treat with care."
    if blend.kalshi_weight < 0.995:
        reason += f" Blend: {blend.describe()}."

    return Pick(side, team, line, edge, p_cover, probs.push, confidence,
                reason + f" Take {team} {spread_text}.")


def _format_line(line: float, side: str) -> str:
    """The number as the chosen side would be bet."""
    value = line if side == "home" else -line
    return f"{-value:+.1f}" if value else "PK"


def _margin_text(margin: float, home_team: str, away_team: str) -> str:
    if abs(margin) < 0.05:
        return "a pick'em"
    team = home_team if margin > 0 else away_team
    return f"{team} -{abs(margin):.1f}"
