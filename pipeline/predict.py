"""Turn an estimate of the fair spread plus a line into a pick, or a refusal.

Two modes, deliberately different:

  master -- the tool's official pick. Carries every safety rule: an unreadable
            ladder is suppressed, a line sitting inside the market's own
            uncertainty band is a no-play, and the estimate is first shrunk
            toward the line.

  lens   -- one signal on its own, graded so its record can be compared with the
            others. Only the vig floor applies. No band test, no tier gate, no
            shrinkage: a lens exists to measure whether that signal carries
            information at all, and filtering it through the master's rules (or
            pulling it toward the very line it is being judged against) would
            destroy the thing being measured.

So the master makes fewer picks than the lenses by design, not by accident.

INVARIANT: docs/app.js reimplements this so a hand-typed line is judged by the
same rules. The rule set is kept small so that stays cheap; pipeline/tests pins
this side.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Optional

from . import config
from .margin_model import cover_probabilities


@dataclass
class Pick:
    side: Optional[str]              # "home" | "away" | None when no play
    team: Optional[str]
    line: float                      # home-perspective, + means home gives points
    estimate: Optional[float]        # the fair spread this pick was made from
    edge: float                      # estimate minus the line
    p_cover: Optional[float]
    p_push: float
    confidence: str                  # no-signal | no-play | lean | solid | strong
    reason: str
    strategy_version: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None or k in
                ("side", "team", "estimate", "p_cover")}


def _no(line, estimate, edge, p_cover, p_push, confidence, reason) -> Pick:
    return Pick(None, None, line or 0.0, estimate, edge, p_cover, p_push, confidence, reason)


def evaluate(margin: Optional[float], line: Optional[float],
             home_team: str, away_team: str, *,
             mode: str = "master",
             read=None, quality=None,
             band: Optional[tuple] = None,
             unavailable: Optional[str] = None,
             blend_label: Optional[str] = None) -> Pick:
    """Decide whether `margin` disagrees with `line` by enough to act on."""

    # 1. No estimate at all.
    if margin is None:
        return _no(line, None, 0.0, None, 0.0, "no-signal",
                   f"Insufficient market: {unavailable or 'no estimate available'}.")

    # 2. Master only: a ladder too thin to read produces no pick, ever.
    if mode == "master" and quality is not None and quality.tier == "D":
        why = "; ".join(quality.reasons) or "unreadable ladder"
        return _no(line, margin, 0.0, None, 0.0, "no-signal", f"Insufficient market: {why}.")

    if line is None:
        return _no(None, margin, 0.0, None, 0.0, "no-play",
                   "No Vegas line available - enter one to grade this game.")

    edge = margin - line

    # P(cover) comes from the ladder's curve shifted so its median sits on this
    # estimate. Without a readable ladder there is no distribution to read, so
    # the pick still stands on the edge alone and the probability is left blank
    # rather than invented.
    probs = None
    if read is not None and not read.rejected:
        probs = cover_probabilities(read.curve_mid, line - (margin - read.implied_margin))

    p_push = probs.push if probs else 0.0
    best = probs.best if probs else None

    # 3. Master only: an edge smaller than the market's own uncertainty about
    #    where the line sits is not an edge.
    if mode == "master" and band is not None and band[0] <= line <= band[1]:
        return _no(line, margin, edge, best, p_push, "no-play",
                   f"Line sits inside the market's own {abs(band[1] - band[0]):.1f}pt "
                   f"uncertainty band ({band[0]:+.1f} to {band[1]:+.1f}); "
                   "no edge worth acting on.")

    # 4. Both modes: an edge under the vig is not worth betting.
    if abs(edge) < config.MIN_EDGE_POINTS:
        return _no(line, margin, edge, best, p_push, "no-play",
                   f"Market and line agree to within {abs(edge):.1f} pts. Anything under "
                   f"{config.MIN_EDGE_POINTS:.0f} pt is inside the vig, so there is "
                   "nothing to bet here.")

    side = "home" if edge > 0 else "away"
    team = home_team if side == "home" else away_team
    p_cover = None
    if probs:
        p_cover = probs.home if side == "home" else probs.away

    unpinned = False
    if read is not None and not read.rejected:
        unpinned = read.strike_distance(line) > config.MAX_STRIKE_DISTANCE

    magnitude = abs(edge)
    if unpinned or magnitude < config.EDGE_SOLID:
        confidence = "lean"
    elif magnitude < config.EDGE_STRONG:
        confidence = "solid"
    else:
        confidence = "strong"
    if mode == "master" and quality is not None and quality.tier == "C":
        confidence = "lean"

    reason = (f"Estimate {_margin_text(margin, home_team, away_team)}; "
              f"line is {_margin_text(line, home_team, away_team)}. ")
    if p_cover is not None:
        reason += f"At that number the market gives {team} a {p_cover * 100:.0f}% chance to cover. "
    reason += f"Edge {magnitude:.1f} pts."
    if unpinned:
        reason += f" Nearest constraining strike is {read.strike_distance(line):.1f} pts away."
    if blend_label:
        reason += f" Blend: {blend_label}."
    reason += f" Take {team} {_format_line(line, side)}."

    return Pick(side, team, line, margin, edge, p_cover, p_push, confidence, reason)


def _format_line(line: float, side: str) -> str:
    """The number as the chosen side would actually be bet."""
    value = line if side == "home" else -line
    return f"{-value:+.1f}" if value else "PK"


def _margin_text(margin: float, home_team: str, away_team: str) -> str:
    if abs(margin) < 0.05:
        return "a pick'em"
    team = home_team if margin > 0 else away_team
    return f"{team} -{abs(margin):.1f}"
