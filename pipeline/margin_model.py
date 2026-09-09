"""Turn a Kalshi strike ladder into an implied margin-of-victory distribution.

Kalshi's KXNCAAFSPREAD series quotes, for each game, a two-sided ladder of
"<Team> wins by over X.5 points" markets.  Each price is a probability, so the
ladder *is* a survival function over the game's margin.  That lets P(cover) at
any Vegas number be read straight off the market instead of coming from an
invented power rating.

Everything here is pure stdlib -- no numpy, no scipy -- so the GitHub Action
needs no dependency install and cannot break on an upstream release.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

from . import config

# Kalshi phrases every market as "<Team> wins by over <N> points".
_SUBTITLE_RE = re.compile(r"^(?P<team>.+?)\s+wins by over\s+(?P<pts>[\d.]+)\s+points?", re.I)


# --------------------------------------------------------------------------
# Data carriers
# --------------------------------------------------------------------------

@dataclass
class Strike:
    """One rung of the ladder: P(<team> wins by more than `threshold`)."""

    ticker: str
    abbrev: str
    team: str
    threshold: float
    bid: float
    ask: float
    open_interest: float
    last_price: float

    @property
    def width(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def traded(self) -> bool:
        return self.last_price > 0

    def usable(self, median_width: float) -> bool:
        """A quote wide enough to be a market-maker placeholder carries no
        information.  Zero open interest is only damning when the quote is
        also wider than typical for this game."""
        if self.width > config.MAX_STRIKE_WIDTH:
            return False
        if self.open_interest <= 0 and self.width > median_width:
            return False
        return not (self.bid <= 0 and self.ask >= 1)


@dataclass
class Ladder:
    """All strikes for one game, oriented home vs away."""

    event_ticker: str
    title: str
    away_team: str
    home_team: str
    away_abbrev: str
    home_abbrev: str
    strikes: list[Strike]
    close_time: Optional[str] = None
    one_sided: bool = False

    @property
    def game_date(self) -> Optional[str]:
        """Calendar date of the game, parsed from the event ticker.

        Kalshi encodes it as YYMMMDD (e.g. ...-26SEP12OKLAMICH).  This is the
        real game date; `close_time` is a settlement deadline that can fall days
        later, so it must never be shown as a kickoff.
        """
        m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", self.event_ticker)
        if not m:
            return None
        months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                  "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
        month = months.get(m.group(2))
        if not month:
            return None
        try:
            import datetime as _dt
            return _dt.date(2000 + int(m.group(1)), month, int(m.group(3))).isoformat()
        except ValueError:
            return None

    @property
    def total_open_interest(self) -> float:
        return sum(s.open_interest for s in self.strikes)

    @property
    def fraction_traded(self) -> float:
        return (sum(1 for s in self.strikes if s.traded) / len(self.strikes)) if self.strikes else 0.0

    @property
    def median_width(self) -> float:
        return _median([s.width for s in self.strikes]) if self.strikes else 1.0

    def usable_strikes(self) -> list[Strike]:
        mw = self.median_width
        return [s for s in self.strikes if s.usable(mw)]


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    v = sorted(vals)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


# --------------------------------------------------------------------------
# Parsing a Kalshi event into a Ladder
# --------------------------------------------------------------------------

def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_ladder(event: dict) -> Optional[Ladder]:
    """Build a Ladder from a Kalshi event with nested markets.

    Kalshi titles read "<Away> vs <Home>: Spread"; the ticker suffix carries a
    team abbreviation plus the strike (e.g. ``...-RUTGBC-BC7``).
    """
    markets = event.get("markets") or []
    if not markets:
        return None

    strikes: list[Strike] = []
    abbrev_to_team: dict[str, str] = {}

    for m in markets:
        threshold = _num(m.get("floor_strike"))
        bid, ask = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars"))
        if threshold is None or bid is None or ask is None or ask < bid:
            continue

        suffix = m["ticker"].rsplit("-", 1)[-1]
        abbrev = re.sub(r"\d+$", "", suffix)
        if not abbrev:
            continue

        subtitle = m.get("yes_sub_title") or m.get("title") or ""
        match = _SUBTITLE_RE.match(subtitle.strip())
        team = match.group("team").strip() if match else abbrev
        abbrev_to_team.setdefault(abbrev, team)

        strikes.append(Strike(
            ticker=m["ticker"],
            abbrev=abbrev,
            team=team,
            threshold=threshold,
            bid=bid,
            ask=ask,
            open_interest=_num(m.get("open_interest_fp")) or 0.0,
            last_price=_num(m.get("last_price_dollars")) or 0.0,
        ))

    if not strikes or not (1 <= len(abbrev_to_team) <= 2):
        return None

    title = event.get("title") or ""
    away_team, home_team = _split_title(title, list(abbrev_to_team.values()) * 2)

    if len(abbrev_to_team) == 2:
        home_abbrev = _abbrev_for(home_team, abbrev_to_team)
        away_abbrev = next(a for a in abbrev_to_team if a != home_abbrev)
        away_name, home_name = abbrev_to_team[away_abbrev], abbrev_to_team[home_abbrev]
    else:
        # A one-sided ladder: the mismatch is big enough that Kalshi only lists
        # the favourite's side.  Still perfectly readable -- it just constrains
        # one tail of the distribution rather than both.  The empty abbreviation
        # for the missing side never matches a strike, so every rung is
        # correctly attributed to the side that is quoted.
        abbrev, name = next(iter(abbrev_to_team.items()))
        if _closer(name, home_team) >= _closer(name, away_team):
            home_abbrev, away_abbrev = abbrev, ""
            home_name, away_name = name, away_team
        else:
            away_abbrev, home_abbrev = abbrev, ""
            away_name, home_name = name, home_team

    return Ladder(
        event_ticker=event.get("event_ticker", ""),
        title=title,
        away_team=away_name,
        home_team=home_name,
        away_abbrev=away_abbrev,
        home_abbrev=home_abbrev,
        strikes=strikes,
        close_time=min((m["close_time"] for m in markets if m.get("close_time")), default=None),
        one_sided=len(abbrev_to_team) == 1,
    )


def _split_title(title: str, teams: list[str]) -> tuple[str, str]:
    """"<Away> vs <Home>: Spread" -> (away, home)."""
    base = title.split(":")[0]
    parts = re.split(r"\s+vs\.?\s+", base, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return (teams + teams)[0], (teams + teams)[1]


def _abbrev_for(team_name: str, abbrev_to_team: dict[str, str]) -> str:
    """Resolve which ladder abbreviation corresponds to a title team name."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    target = norm(team_name)
    for abbrev, name in abbrev_to_team.items():
        if norm(name) == target:
            return abbrev
    # Fall back to the closest name by shared prefix length.
    best, best_score = None, -1
    for abbrev, name in abbrev_to_team.items():
        n = norm(name)
        score = len(os_common_prefix(n, target))
        if score > best_score:
            best, best_score = abbrev, score
    return best or next(iter(abbrev_to_team))


def _closer(name: str, candidate: str) -> int:
    """How strongly a market's team name matches one side of the title."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    a, b = norm(name), norm(candidate)
    if a == b:
        return 10_000
    return len(os_common_prefix(a, b))


def os_common_prefix(a: str, b: str) -> str:
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return a[:i]


# --------------------------------------------------------------------------
# Survival function construction
# --------------------------------------------------------------------------

# Which price to read for the low/high edges of the uncertainty envelope.
#
# S(x) = P(home margin > x).  A HOME strike contributes its own probability, so
# the low edge takes its bid.  An AWAY strike at threshold s contributes
# 1 - p, so the low edge of S needs the *highest* away price -- its ask.
# Pairing bids on both sides would mix a low estimate with a high one and
# understate the true envelope.
_SIDE_PRICES = {
    "low":  {"home": "bid", "away": "ask"},
    "high": {"home": "ask", "away": "bid"},
    "mid":  {"home": "mid", "away": "mid"},
}


def survival_points(ladder: Ladder, mode: str) -> list[tuple[float, float, float]]:
    """Map the ladder onto points on S(x) = P(home_margin > x).

    Returns (x, probability, weight) triples.
    """
    picks = _SIDE_PRICES[mode]
    pts: list[tuple[float, float, float]] = []

    for s in ladder.usable_strikes():
        is_home = s.abbrev == ladder.home_abbrev
        which = picks["home" if is_home else "away"]
        p = {"bid": s.bid, "ask": s.ask, "mid": s.mid}[which]

        if is_home:
            x, prob = s.threshold, p
        else:
            # P(away wins by > s) = p  =>  P(home margin > -s) = 1 - p.
            # Thresholds sit on .5 so P(margin == -s) is zero.
            x, prob = -s.threshold, 1.0 - p

        # Tighter and better-backed quotes carry more weight.
        weight = (1.0 / max(s.width, 0.01)) * (1.0 + math.log1p(max(s.open_interest, 0.0)))
        pts.append((x, _clamp(prob), weight))

    pts.sort(key=lambda t: t[0])
    return pts


def _clamp(p: float, lo: float = 1e-4, hi: float = 1.0 - 1e-4) -> float:
    return max(lo, min(hi, p))


def pava_non_increasing(pts: list[tuple[float, float, float]]) -> list[tuple[float, float]]:
    """Weighted pool-adjacent-violators, enforcing a non-increasing sequence.

    A survival function must be non-increasing in x.  Wide quotes on thin
    strikes routinely violate that by a point or two of probability; PAVA
    projects onto the nearest valid sequence rather than discarding the data.
    """
    if not pts:
        return []

    # Collapse duplicate x values into a single weighted observation first.
    merged: list[list[float]] = []
    for x, y, w in pts:
        if merged and merged[-1][0] == x:
            b = merged[-1]
            tot = b[2] + w
            b[1] = (b[1] * b[2] + y * w) / tot
            b[2] = tot
        else:
            merged.append([x, y, w])

    # Blocks of [sum_wy, sum_w, count]; merge while order is violated.
    blocks: list[list[float]] = []
    for x, y, w in merged:
        blocks.append([y * w, w, 1])
        while len(blocks) > 1:
            prev, cur = blocks[-2], blocks[-1]
            if prev[0] / prev[1] >= cur[0] / cur[1]:
                break
            blocks[-2] = [prev[0] + cur[0], prev[1] + cur[1], prev[2] + cur[2]]
            blocks.pop()

    out: list[tuple[float, float]] = []
    i = 0
    for total_wy, total_w, count in blocks:
        level = total_wy / total_w
        for _ in range(int(count)):
            out.append((merged[i][0], level))
            i += 1
    return out


# --------------------------------------------------------------------------
# Monotone interpolation (Fritsch-Carlson) with logistic tails
# --------------------------------------------------------------------------

class SurvivalCurve:
    """A monotone non-increasing S(x) = P(home margin > x)."""

    def __init__(self, knots: list[tuple[float, float]]):
        if len(knots) < 2:
            raise ValueError("need at least two knots")
        self.xs = [k[0] for k in knots]
        self.ys = [k[1] for k in knots]
        self._slopes = _fritsch_carlson(self.xs, self.ys)
        self._lo_tail = _fit_logistic(self.xs[:2], self.ys[:2])
        self._hi_tail = _fit_logistic(self.xs[-2:], self.ys[-2:])

    def at(self, x: float) -> float:
        """Evaluate S(x), extrapolating with a fitted logistic past the ends."""
        if x < self.xs[0]:
            return _clamp(_logistic(x, self._lo_tail), 1e-6, 1 - 1e-9) if self._lo_tail else self.ys[0]
        if x > self.xs[-1]:
            return _clamp(_logistic(x, self._hi_tail), 1e-9, 1 - 1e-6) if self._hi_tail else self.ys[-1]

        i = _bracket(self.xs, x)
        h = self.xs[i + 1] - self.xs[i]
        if h <= 0:
            return self.ys[i]
        t = (x - self.xs[i]) / h
        y0, y1 = self.ys[i], self.ys[i + 1]
        m0, m1 = self._slopes[i], self._slopes[i + 1]
        # Cubic Hermite basis.
        return (
            y0 * (2 * t**3 - 3 * t**2 + 1)
            + h * m0 * (t**3 - 2 * t**2 + t)
            + y1 * (-2 * t**3 + 3 * t**2)
            + h * m1 * (t**3 - t**2)
        )

    def median(self) -> Optional[float]:
        """The x where S(x) = 0.5 -- the market's implied home margin."""
        return self.quantile(0.5)

    def quantile(self, p: float) -> Optional[float]:
        lo, hi = self.xs[0] - 60.0, self.xs[-1] + 60.0
        if self.at(lo) < p or self.at(hi) > p:
            return None
        for _ in range(200):
            mid = (lo + hi) / 2.0
            if self.at(mid) > p:
                lo = mid
            else:
                hi = mid
            if hi - lo < 1e-6:
                break
        return (lo + hi) / 2.0

    def table(self, lo: int = config.MARGIN_MIN, hi: int = config.MARGIN_MAX) -> list[float]:
        """Dense S(x) on the HALF-integer grid, for the browser to look up.

        Margins are integers, so P(M > 7) and P(M > 7.5) are the same event and
        must return the same number -- something a smooth curve evaluated at
        both points will not do.  Half-integers are also exactly where Kalshi
        quotes ("wins by over 7.5"), so every stored value sits on a real
        market price rather than between two of them.
        """
        vals = [self.at(x + 0.5) for x in range(lo, hi)]
        # Guarantee monotonicity survives rounding for the consumer.
        for i in range(1, len(vals)):
            vals[i] = min(vals[i], vals[i - 1])
        return [round(max(0.0, min(1.0, v)), 5) for v in vals]


def _bracket(xs: list[float], x: float) -> int:
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    return lo


def _fritsch_carlson(xs: list[float], ys: list[float]) -> list[float]:
    """Monotone cubic Hermite tangents (PCHIP), preserving non-increase."""
    n = len(xs)
    if n == 2:
        d = (ys[1] - ys[0]) / (xs[1] - xs[0])
        return [d, d]

    deltas = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
    m = [0.0] * n
    m[0], m[-1] = deltas[0], deltas[-1]
    for i in range(1, n - 1):
        if deltas[i - 1] * deltas[i] <= 0:
            m[i] = 0.0          # local extremum -> flatten to preserve monotonicity
        else:
            m[i] = (deltas[i - 1] + deltas[i]) / 2.0

    # Fritsch-Carlson limiter.
    for i in range(n - 1):
        if deltas[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / deltas[i], m[i + 1] / deltas[i]
        s = a * a + b * b
        if s > 9:
            tau = 3.0 / math.sqrt(s)
            m[i] = tau * a * deltas[i]
            m[i + 1] = tau * b * deltas[i]
    return m


def _fit_logistic(xs: list[float], ys: list[float]) -> Optional[tuple[float, float]]:
    """Fit S(x) = 1/(1+exp((x-mu)/s)) through two points; returns (mu, s)."""
    (x1, y1), (x2, y2) = zip(xs, ys)
    if x1 == x2:
        return None
    try:
        l1, l2 = math.log(1 / y1 - 1), math.log(1 / y2 - 1)
    except (ValueError, ZeroDivisionError):
        return None
    if l2 == l1:
        return None
    s = (x2 - x1) / (l2 - l1)
    if s <= 0:
        return None
    return x1 - s * l1, s


def _logistic(x: float, params: tuple[float, float]) -> float:
    mu, s = params
    z = (x - mu) / s
    if z > 60:
        return 0.0
    if z < -60:
        return 1.0
    return 1.0 / (1.0 + math.exp(z))


def build_curve(ladder: Ladder, mode: str) -> Optional[SurvivalCurve]:
    pts = survival_points(ladder, mode)
    knots = pava_non_increasing(pts)
    if len(knots) < 2:
        return None
    try:
        return SurvivalCurve(knots)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Reading a line off the curve
# --------------------------------------------------------------------------

@dataclass
class CoverProbabilities:
    """How a given spread resolves against the implied margin distribution.

    `line` is expressed as points the HOME team gives: +7.5 means home is
    favoured by 7.5, -3 means home is a 3-point underdog.
    """

    line: float
    home: float
    away: float
    push: float

    @property
    def favored_side(self) -> str:
        return "home" if self.home >= self.away else "away"

    @property
    def best(self) -> float:
        return max(self.home, self.away)


def cover_probabilities(curve: SurvivalCurve, line: float) -> CoverProbabilities:
    """P(home covers), P(away covers), P(push) for a home-perspective line."""
    if abs(line - round(line)) < 1e-9:
        # A whole number can push.  Margins are integers, so
        # P(margin > n) == P(margin > n + 0.5) and P(margin == n) is the gap
        # between the two neighbouring half-point strikes.
        n = float(round(line))
        above, below = curve.at(n + 0.5), curve.at(n - 0.5)
        home = above
        push = max(0.0, below - above)
        away = max(0.0, 1.0 - below)
    else:
        home = curve.at(line)
        push = 0.0
        away = max(0.0, 1.0 - home)

    total = home + away + push
    if total > 0:                          # renormalise against interpolation drift
        home, away, push = home / total, away / total, push / total
    return CoverProbabilities(line=line, home=home, away=away, push=push)


@dataclass
class MarketRead:
    """Everything the model can say about one game's ladder."""

    ladder: Ladder
    curve_mid: SurvivalCurve
    curve_low: SurvivalCurve
    curve_high: SurvivalCurve
    implied_margin: float                  # home-perspective, positive = home favoured
    margin_low: float
    margin_high: float
    usable_strikes: int
    total_strikes: int
    rejected: Optional[str] = None
    _table: list[float] = field(default_factory=list)

    @property
    def band(self) -> float:
        """The market's own uncertainty about where the line sits, in points.

        Derived from the bid/ask envelope rather than a hand-tuned constant,
        and measured at the median crossing -- which is where it matters, and
        where a raw bid-ask-width filter misses games whose *wide* strikes
        happen to sit right at the crossing.
        """
        return abs(self.margin_high - self.margin_low)

    def strike_distance(self, line: float) -> float:
        """Distance from `line` to the nearest usable strike constraining it."""
        mw = self.ladder.median_width
        xs = []
        for s in self.ladder.strikes:
            if not s.usable(mw):
                continue
            xs.append(s.threshold if s.abbrev == self.ladder.home_abbrev else -s.threshold)
        return min((abs(x - line) for x in xs), default=float("inf"))

    def strike_span(self) -> tuple[float, float]:
        """Signed margin range the usable strikes actually constrain.

        Outside this range the curve is a fitted logistic tail, not market
        data, so conclusions drawn there deserve much less confidence.
        """
        mw = self.ladder.median_width
        xs = [
            (s.threshold if s.abbrev == self.ladder.home_abbrev else -s.threshold)
            for s in self.ladder.strikes if s.usable(mw)
        ]
        return (min(xs), max(xs)) if xs else (0.0, 0.0)

    def table(self) -> list[float]:
        if not self._table:
            self._table = self.curve_mid.table()
        return self._table


def read_market(ladder: Ladder) -> MarketRead:
    """Build all three curves and the uncertainty band, or explain the refusal."""
    usable = ladder.usable_strikes()

    def fail(reason: str) -> MarketRead:
        empty = SurvivalCurve([(-1.0, 0.51), (1.0, 0.49)])
        return MarketRead(ladder, empty, empty, empty, 0.0, 0.0, 0.0,
                          len(usable), len(ladder.strikes), rejected=reason)

    if len(usable) < config.MIN_USABLE_STRIKES:
        return fail(f"only {len(usable)} usable strikes of {len(ladder.strikes)}")

    curves = {m: build_curve(ladder, m) for m in ("mid", "low", "high")}
    if any(c is None for c in curves.values()):
        return fail("could not fit a monotone curve")

    medians = {m: c.median() for m, c in curves.items()}
    if any(v is None for v in medians.values()):
        return fail("survival curve never crosses 0.5")

    # The envelope edges can invert when both cross inside one strike interval.
    lo, hi = sorted((medians["low"], medians["high"]))

    return MarketRead(
        ladder=ladder,
        curve_mid=curves["mid"], curve_low=curves["low"], curve_high=curves["high"],
        implied_margin=medians["mid"], margin_low=lo, margin_high=hi,
        usable_strikes=len(usable), total_strikes=len(ladder.strikes),
    )
