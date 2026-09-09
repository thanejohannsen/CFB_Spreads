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

    @property
    def tradeable(self) -> bool:
        return self.tier in ("A", "B", "C")


def grade(read: MarketRead) -> Quality:
    """Assign a quality tier from the uncertainty band, open interest and
    how much of the ladder has actually traded."""
    ladder = read.ladder
    oi = ladder.total_open_interest
    traded = ladder.fraction_traded
    reasons: list[str] = []

    if read.rejected:
        return Quality("D", config.TIER_D["label"], float("inf"), oi, traded,
                       read.usable_strikes, [read.rejected])

    band = read.band

    if band > config.MAX_TRADEABLE_BAND:
        reasons.append(f"{band:.1f}pt uncertainty band")
    if oi < config.MIN_OPEN_INTEREST:
        reasons.append(f"only {oi:,.0f} open interest")
    if reasons:
        return Quality("D", config.TIER_D["label"], band, oi, traded,
                       read.usable_strikes, reasons)

    for tier in ("A", "B", "C"):
        t = config.TIERS[tier]
        if band <= t["band"] and oi >= t["oi"] and traded >= t["traded"]:
            return Quality(tier, t["label"], band, oi, traded, read.usable_strikes, [])

    # Inside the tradeable band but short of C's floors.
    return Quality("C", config.TIERS["C"]["label"], band, oi, traded, read.usable_strikes, [])


# --------------------------------------------------------------------------
# Shrinkage
# --------------------------------------------------------------------------

def weight_for_band(band: float) -> float:
    """Share of the estimate that a ladder with this band earns against the prior.

    Depends only on the band, so the page can reproduce the blend for any line
    the user types without re-deriving anything.
    """
    sigma = max(band, config.MIN_BAND_SIGMA)
    w_kalshi = 1.0 / (sigma ** 2)
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

    sigma_kalshi = max(read.band, config.MIN_BAND_SIGMA)
    w_kalshi = 1.0 / (sigma_kalshi ** 2)
    w_prior = 1.0 / (config.PRIOR_SIGMA ** 2)
    total = w_kalshi + w_prior

    blended = (w_kalshi * read.implied_margin + w_prior * prior_margin) / total
    return Blend(blended, w_kalshi / total, prior_margin, prior_source)
