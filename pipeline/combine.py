"""Combine the tool's signals into one estimate of the fair spread.

Each signal is an estimate of the same thing -- how many points the home team
gives -- carrying its own standard error in points.  That makes them directly
comparable, which is the whole reason the moneyline is converted onto the spread
scale rather than left as a probability.

Why precision weighting rather than a fixed preference for one signal: the two
Kalshi markets trade off in a way that depends on the game.  The moneyline is
quoted at margin zero, so on a lopsided game its estimate has to be dragged
along the curve to reach the spread, amplifying its error; the ladder has real
traded strikes sitting at the number itself.  On a near pick'em the moneyline is
quoted right where the spread lives and competes well.  Measured across a live
slate the ladder's share ranged 26%-99% (median 85%), so a fixed rule either way
would be wrong a good share of the time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import config


@dataclass
class Signal:
    """One estimate of the fair spread, home-perspective, with its precision."""

    key: str
    label: str
    margin: Optional[float]
    sigma: Optional[float]
    note: str = ""

    @property
    def available(self) -> bool:
        return self.margin is not None and self.sigma is not None

    @property
    def weight(self) -> float:
        if not self.available:
            return 0.0
        return 1.0 / (max(self.sigma, config.MIN_BAND_SIGMA) ** 2)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "margin": None if self.margin is None else round(self.margin, 2),
            "sigma": None if self.sigma is None else round(self.sigma, 3),
            "note": self.note,
        }


@dataclass
class Composite:
    """A precision-weighted blend of several signals."""

    margin: Optional[float]
    sigma: Optional[float]
    shares: dict[str, float]
    used: list[str]

    def percent_shares(self) -> dict[str, int]:
        """Whole-percent shares that sum to exactly 100.

        Rounding each share independently lets them total 99 or 101, which looks
        like a bug on screen. Largest-remainder keeps the displayed numbers
        honest, and emitting them once means the page and the stored reason can
        never disagree by a point.
        """
        if not self.used:
            return {}
        raw = {k: self.shares[k] * 100 for k in self.used}
        out = {k: int(v) for k, v in raw.items()}
        short = 100 - sum(out.values())
        for k in sorted(raw, key=lambda k: raw[k] - int(raw[k]), reverse=True)[:short]:
            out[k] += 1
        return out

    def describe(self) -> str:
        if not self.used:
            return "no signal"
        pct = self.percent_shares()
        return " / ".join(f"{pct[k]}% {k.replace('_', ' ')}" for k in self.used)

    def to_dict(self) -> dict:
        return {
            "margin": None if self.margin is None else round(self.margin, 2),
            "sigma": None if self.sigma is None else round(self.sigma, 3),
            "shares": self.percent_shares(),          # whole percents, summing to 100
            "used": self.used,
            "label": self.describe(),
        }


def combine(signals: list[Signal]) -> Composite:
    """Precision-weighted average, with a floor on the combined uncertainty.

    The floor is the important part.  Inverse-variance weighting assumes the
    inputs err independently; the ladder and the moneyline are the same exchange
    and largely the same traders, so their errors are heavily correlated and the
    textbook 1/sqrt(sum of weights) would report the blend as sharper than the
    sharper of its two inputs.  Averaging the point estimates is still the right
    move -- claiming the variance shrank is not.
    """
    usable = [s for s in signals if s.available]
    if not usable:
        return Composite(None, None, {}, [])

    total = sum(s.weight for s in usable)
    margin = sum(s.weight * s.margin for s in usable) / total
    sigma = min(max(s.sigma, config.MIN_BAND_SIGMA) for s in usable)

    return Composite(
        margin=margin,
        sigma=sigma,
        shares={s.key: s.weight / total for s in usable},
        used=[s.key for s in usable],
    )


# --------------------------------------------------------------------------
# Building the signals for one game
# --------------------------------------------------------------------------

def ladder_signal(read) -> Signal:
    """The spread ladder's own median, with half its bid/ask envelope."""
    if read.rejected:
        return Signal("kalshi_spread", "Kalshi spread", None, None, read.rejected)
    return Signal("kalshi_spread", "Kalshi spread", read.implied_margin, read.band / 2.0,
                  f"{read.usable_strikes} usable strikes")


def moneyline_signal(cross) -> Signal:
    """The moneyline converted onto the spread scale, with its own bid/ask."""
    if cross is None or cross.ml_margin is None or cross.ml_band is None:
        why = "no comparable moneyline" if cross is None else cross.note
        return Signal("kalshi_ml", "Kalshi moneyline", None, None, why)
    return Signal("kalshi_ml", "Kalshi moneyline", cross.ml_margin, cross.ml_band / 2.0,
                  f"{cross.ml_win_prob * 100:.1f}% win probability")


def sp_plus_signal(projection) -> Signal:
    """SP+ ratings. Deliberately excluded from the master composite."""
    if projection is None:
        return Signal("sp_plus", "SP+", None, None, "no SP+ rating for one of these teams")
    return Signal("sp_plus", "SP+", projection.home_favored_by, config.SP_PLUS_SIGMA,
                  projection.describe())


def master_composite(ladder: Signal, moneyline: Signal) -> Composite:
    """The master pick's estimate: the two Kalshi markets only.

    SP+ is excluded on purpose. It grades near the break-even a -110 line
    demands, so letting it move a number backed by hundreds of thousands of
    contracts would add noise, not information. It keeps its own tab and record
    and can be promoted later if that record earns it.
    """
    return combine([ladder, moneyline])
