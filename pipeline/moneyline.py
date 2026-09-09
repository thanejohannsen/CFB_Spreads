"""Cross-check the spread ladder against Kalshi's own moneyline market.

The spread ladder already contains a win probability: ties are impossible in
college football, so P(home wins) is just S(0).  Kalshi also runs a separate
moneyline market on the same game (KXNCAAFGAME), and every spread event has a
matching one on an identical ticker key.

Comparing them is a stronger signal than Kalshi-vs-Vegas: it is the same
exchange and the same participants, so a gap cannot be explained away by vig or
by two books disagreeing.  It is also asymmetric in a useful way -- the
moneyline markets carry far more open interest than the spread ladders (over a
million contracts against tens of thousands on the same game), so when the two
disagree it is usually the thinner spread ladder that is mispriced.

The conversion runs both ways:

  spread -> moneyline   P(home wins) = S(0), shown as a probability and as
                        American odds.
  moneyline -> spread   Shift the game's own margin distribution by delta so
                        that P(M + delta > 0) equals the moneyline probability,
                        then read off the new median.  Using the game's actual
                        distribution shape beats a generic win-probability
                        lookup table, because it respects how that specific
                        matchup's margins are dispersed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from . import config
from .http import get_json
from .margin_model import Ladder, MarketRead

# Below/above these probabilities the points-equivalent conversion is unstable
# and the comparison stops being informative.
MIN_PROB = 0.03
MAX_PROB = 0.97

# Report a divergence only once it clears the combined uncertainty of both
# markets by this margin, in points.
DIVERGENCE_SLACK = 0.5


@dataclass
class MoneylineQuote:
    home_prob: float
    away_prob: float
    home_prob_low: float
    home_prob_high: float
    open_interest: float

    @property
    def band(self) -> float:
        return abs(self.home_prob_high - self.home_prob_low)


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_quotes() -> dict[str, MoneylineQuote]:
    """Win probabilities per game, keyed by the shared ticker key.

    Kalshi runs the two sides as separate binary markets, so the pair is
    renormalised to sum to one -- that removes any residual spread between them.
    """
    events, cursor = [], None
    for _ in range(20):
        payload = get_json(f"{config.KALSHI_BASE}/events", {
            "series_ticker": "KXNCAAFGAME", "status": "open",
            "limit": 200, "with_nested_markets": "true", "cursor": cursor,
        })
        events.extend(payload.get("events", []))
        cursor = payload.get("cursor")
        if not cursor:
            break

    out: dict[str, MoneylineQuote] = {}
    for event in events:
        key = event.get("event_ticker", "").replace("KXNCAAFGAME-", "")
        title = event.get("title") or ""
        sides = {}
        for m in event.get("markets", []):
            bid, ask = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars"))
            if bid is None or ask is None:
                continue
            abbrev = re.sub(r"\d+$", "", m["ticker"].rsplit("-", 1)[-1])
            sides[abbrev] = {
                "bid": bid, "ask": ask, "mid": (bid + ask) / 2.0,
                "team": (m.get("yes_sub_title") or "").strip(),
                "oi": _num(m.get("open_interest_fp")) or 0.0,
            }
        if len(sides) != 2:
            continue

        home_abbrev = _home_abbrev(title, sides)
        if home_abbrev is None:
            continue
        away_abbrev = next(a for a in sides if a != home_abbrev)
        home, away = sides[home_abbrev], sides[away_abbrev]

        total = home["mid"] + away["mid"]
        if total <= 0:
            continue

        # Envelope: cheapest home paired with dearest away, and vice versa.
        lo_total = home["bid"] + away["ask"]
        hi_total = home["ask"] + away["bid"]
        out[key] = MoneylineQuote(
            home_prob=home["mid"] / total,
            away_prob=away["mid"] / total,
            home_prob_low=(home["bid"] / lo_total) if lo_total > 0 else home["bid"],
            home_prob_high=(home["ask"] / hi_total) if hi_total > 0 else home["ask"],
            open_interest=home["oi"] + away["oi"],
        )
    return out


def _home_abbrev(title: str, sides: dict) -> Optional[str]:
    """Kalshi titles read "<Away> vs <Home>"; match the home name to its side."""
    base = title.split(":")[0]
    parts = re.split(r"\s+vs\.?\s+", base, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    home = norm(parts[1])
    best, best_score = None, -1
    for abbrev, side in sides.items():
        n = norm(side["team"])
        score = 10_000 if n == home else _prefix(n, home)
        if score > best_score:
            best, best_score = abbrev, score
    return best


def _prefix(a: str, b: str) -> int:
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return i


def american_odds(p: float) -> Optional[int]:
    """Probability -> American moneyline odds."""
    if not 0 < p < 1:
        return None
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))


@dataclass
class CrossCheck:
    """How the spread ladder and the moneyline market compare."""

    spread_win_prob: float          # P(home wins) implied by the ladder
    ml_win_prob: float              # P(home wins) from the moneyline market
    spread_margin: float            # ladder median margin, home perspective
    ml_margin: Optional[float]      # margin the moneyline is pricing
    divergence_pts: Optional[float]
    divergence_prob: float
    ml_open_interest: float
    significant: bool
    note: str

    def to_dict(self) -> dict:
        return {
            "spread_win_prob": round(self.spread_win_prob, 4),
            "ml_win_prob": round(self.ml_win_prob, 4),
            "spread_odds": american_odds(self.spread_win_prob),
            "ml_odds": american_odds(self.ml_win_prob),
            "spread_margin": round(self.spread_margin, 2),
            "ml_margin": None if self.ml_margin is None else round(self.ml_margin, 2),
            "divergence_pts": None if self.divergence_pts is None else round(self.divergence_pts, 2),
            "divergence_prob": round(self.divergence_prob, 4),
            "ml_open_interest": round(self.ml_open_interest),
            "significant": self.significant,
            "note": self.note,
        }


def cross_check(read: MarketRead, quote: Optional[MoneylineQuote]) -> Optional[CrossCheck]:
    """Compare the ladder's own win probability with the moneyline market's."""
    if quote is None or read.rejected:
        return None

    # Ties are impossible, so P(home wins) == P(margin > 0).
    spread_prob = read.curve_mid.at(0.0)
    ml_prob = quote.home_prob
    divergence_prob = ml_prob - spread_prob

    if not (MIN_PROB <= ml_prob <= MAX_PROB and MIN_PROB <= spread_prob <= MAX_PROB):
        return CrossCheck(
            spread_prob, ml_prob, read.implied_margin, None, None, divergence_prob,
            quote.open_interest, False,
            "One side is priced near certainty; a points-equivalent comparison "
            "is not meaningful here.",
        )

    # Shift the game's own margin distribution until P(margin > 0) matches the
    # moneyline, then read the new median.  quantile(p) is the x where S(x)=p,
    # so the required shift is exactly -quantile(p_ml).
    shift_point = read.curve_mid.quantile(ml_prob)
    if shift_point is None:
        return None
    ml_margin = read.implied_margin - shift_point
    divergence_pts = ml_margin - read.implied_margin

    # Only call it significant once it clears both markets' own uncertainty.
    ml_band_pts = abs(quote.band) * _points_per_prob(read)
    threshold = read.band + ml_band_pts + DIVERGENCE_SLACK
    significant = abs(divergence_pts) > threshold

    # A conversion landing outside the range the strikes actually cover is
    # reading a fitted tail rather than traded prices.  Near the tails the
    # slope of margin against probability is steep, so a fraction of a point of
    # probability noise turns into several points of apparent disagreement.
    lo, hi = read.strike_span()
    extrapolated = not (lo - 1.0 <= ml_margin <= hi + 1.0)
    if extrapolated and significant:
        significant = False
        return CrossCheck(
            spread_prob, ml_prob, read.implied_margin, ml_margin,
            divergence_pts, divergence_prob, quote.open_interest, False,
            f"Moneyline implies {_fmt(ml_margin, read.ladder)}, beyond the "
            f"{lo:+.0f} to {hi:+.0f} range the ladder's strikes actually cover - "
            "the gap is extrapolation, not a tradeable disagreement.",
        )

    if significant:
        richer = "moneyline" if quote.open_interest > read.ladder.total_open_interest else "spread"
        note = (f"Moneyline prices this like {_fmt(ml_margin, read.ladder)}, "
                f"the ladder like {_fmt(read.implied_margin, read.ladder)} - "
                f"a {abs(divergence_pts):.1f}pt disagreement inside Kalshi. "
                f"The {richer} market carries more open interest, so favour it.")
    else:
        note = (f"Moneyline and ladder agree within {threshold:.1f}pts "
                f"({abs(divergence_pts):.1f}pt gap).")

    return CrossCheck(spread_prob, ml_prob, read.implied_margin, ml_margin,
                      divergence_pts, divergence_prob, quote.open_interest,
                      significant, note)


def _points_per_prob(read: MarketRead) -> float:
    """Local slope of margin with respect to win probability, in points.

    Used to express the moneyline's bid/ask uncertainty on the same points
    scale as the ladder's band, so the two are compared like for like.
    """
    hi, lo = read.curve_mid.quantile(0.45), read.curve_mid.quantile(0.55)
    if hi is None or lo is None:
        return 30.0
    return abs(hi - lo) / 0.10


def _fmt(margin: float, ladder: Ladder) -> str:
    if abs(margin) < 0.05:
        return "a pick'em"
    team = ladder.home_team if margin > 0 else ladder.away_team
    return f"{team} -{abs(margin):.1f}"
