"""Ladder quality tiering and shrinkage toward a prior.

Kalshi's college football markets are thin until close to kickoff, but picks
are due Wednesday night.  Two mechanisms handle that honestly:

  * tiering  -- grade how readable each ladder is, and refuse to pick from one
                that cannot support a pick;
  * shrinkage -- rather than falling off a cliff at the tier boundary, pull the
                estimate toward the Vegas prior in proportion to how uncertain
                the ladder is, and say so on the page.

Measured on a Wednesday against the following Saturday's slate, the 30 most
popular games had a median band of 0.30 pts and a maximum of 0.86 -- so within
the tool's scope shrinkage is a safety net, not a load-bearing component.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import config
from .margin_model import MarketRead


@dataclass
class Quality:
    tier: str
    label: str
    band: float
    open_interest: float
    fraction_traded: float
    usable_strikes: int
    reasons: list[str]
    dollar_volume: float = 0.0

    @property
    def tradeable(self) -> bool:
        return self.tier in ("S", "A", "B", "C")


def grade(read: MarketRead, dollar_volume: float = 0.0) -> Quality:
    """Assign a quality tier from the uncertainty band, open interest, how much
    of the ladder has traded, and the money on the game.

    `dollar_volume` is the combined spread + moneyline figure; the ladder alone
    never reaches the S threshold, so passing only the ladder's own volume would
    make that tier unreachable."""
    ladder = read.ladder
    oi = ladder.total_open_interest
    traded = ladder.fraction_traded
    reasons: list[str] = []

    if read.rejected:
        return Quality("D", config.TIER_D["label"], float("inf"), oi, traded,
                       read.usable_strikes, [read.rejected], dollar_volume)

    band = read.band

    if band > config.MAX_TRADEABLE_BAND:
        reasons.append(f"{band:.1f}pt uncertainty band")
    if oi < config.MIN_OPEN_INTEREST:
        reasons.append(f"only {oi:,.0f} open interest")
    if reasons:
        return Quality("D", config.TIER_D["label"], band, oi, traded,
                       read.usable_strikes, reasons, dollar_volume)

    for tier in ("A", "B", "C"):
        t = config.TIERS[tier]
        if band <= t["band"] and oi >= t["oi"] and traded >= t["traded"]:
            # S is A plus serious money. Requiring A's bars as well means a
            # heavily traded game with a loose band cannot outrank a tight one;
            # in practice the volume gate is the binding constraint.
            if tier == "A" and dollar_volume >= config.S_TIER_DOLLAR_VOLUME:
                tier, t = "S", config.TIER_S
            return Quality(tier, t["label"], band, oi, traded,
                           read.usable_strikes, [], dollar_volume)

    # Inside the tradeable band but short of C's floors.
    return Quality("C", config.TIERS["C"]["label"], band, oi, traded,
                   read.usable_strikes, [], dollar_volume)


# --------------------------------------------------------------------------
# Shrinkage
# --------------------------------------------------------------------------

def sigma_for_band(band: float) -> float:
    """The standard error a band of this width implies, floored.

    A band runs from one edge of the market's uncertainty to the other, so the
    distance from its centre out to an edge -- which is what a sigma is -- is
    half of it.  Everything in the tool that turns an interval into a sigma
    comes through here, so the ladder, the moneyline and the Vegas prior are all
    weighted on the same definition.  combine.py halves the moneyline's band the
    same way; before this existed, this module fed the whole band in as a sigma
    and quietly gave the prior four times the weight it had earned.
    """
    return max(band / 2.0, config.MIN_BAND_SIGMA)


def weight_for_sigma(sigma: float) -> float:
    """Share of the estimate a Kalshi read this precise earns against the prior.

    Depends only on the sigma, so the page can reproduce the blend for any line
    the user types without re-deriving anything.
    """
    w_kalshi = 1.0 / (max(sigma, config.MIN_BAND_SIGMA) ** 2)
    return w_kalshi / (w_kalshi + 1.0 / (config.PRIOR_SIGMA ** 2))


@dataclass
class Blend:
    """A precision-weighted combination of the Kalshi read and a prior."""

    margin: float
    kalshi_weight: float
    prior_margin: Optional[float]
    prior_source: Optional[str]

    @property
    def prior_weight(self) -> float:
        return 1.0 - self.kalshi_weight

    def describe(self) -> str:
        if self.prior_margin is None or self.kalshi_weight >= 0.995:
            return "100% Kalshi"
        return (f"{self.kalshi_weight * 100:.0f}% Kalshi / "
                f"{self.prior_weight * 100:.0f}% {self.prior_source or 'prior'}")


def shrink(read: MarketRead, prior_margin: Optional[float],
           prior_source: str = "Vegas prior") -> Blend:
    """Pull the Kalshi estimate toward `prior_margin` by relative precision.

    A tight ladder (band ~0.3 pt) is essentially all Kalshi.  A loose one lands
    most of the way on the prior, which is the honest answer: the market has no
    information here beyond what Vegas already says.  Tier D is suppressed
    upstream -- shrinkage must never launder a prior as a Kalshi prediction.
    """
    if prior_margin is None:
        return Blend(read.implied_margin, 1.0, None, None)

    w_kalshi = 1.0 / (sigma_for_band(read.band) ** 2)
    w_prior = 1.0 / (config.PRIOR_SIGMA ** 2)
    total = w_kalshi + w_prior

    blended = (w_kalshi * read.implied_margin + w_prior * prior_margin) / total
    return Blend(blended, w_kalshi / total, prior_margin, prior_source)
