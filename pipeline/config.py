"""Central configuration for the CFB spreads pipeline.

Every threshold here was calibrated against real Kalshi data measured on a
Wednesday afternoon against the following Saturday's slate -- i.e. at the
decision point, not at the kickoff liquidity peak.  See README for the
measurements behind each number.
"""

# =========================================================== TUNABLE =======
# Everything in this block is a judgement call rather than a measurement.
# Change these to re-tune the tool; everything else is derived from data.
#
# Bump STRATEGY_VERSION whenever the master pick's formula changes.  It is
# stamped into each locked pick so a mid-season change can be split out of the
# record instead of silently blending two different rule sets.  The three lens
# records need no version: their definitions never change.
# v2 (2026-09-16): every probability<->points conversion moved off the strike
# curve's local slope onto a scale fitted across the whole ladder. The old
# slope was 1c-tick noise -- it ran 1.5% to 10.0% per point across one slate --
# and it set the moneyline's sigma, which the composite weights as 1/sigma^2.
# On a single market snapshot the correction moved 17 of 30 moneyline lens
# picks, one of them to the other side, so this is a formula change and not a
# tidy-up. v1 rows stay separable in the record.
STRATEGY_VERSION = "v2"

# ------------------------------------------------- publication precision ----
#
# These are NOT cosmetic.  docs/app.js re-runs the pick rules over the JSON this
# pipeline commits, so the page can only ever see values at the precision they
# were written at.  If the pipeline decides from a full-precision float and then
# publishes a rounded one, the two disagree whenever a number lands within half
# a unit of the last decimal place of a threshold -- and the board then tells
# you to pass on a game the record counts as a pick.
#
# The page is authoritative: build_predictions quantises to these before calling
# evaluate, so both sides decide from identical numbers.  Changing one of these
# changes which picks get made at boundaries.
PUBLISH_MARGIN_DP = 2     # margins, bands and spreads, in points
PUBLISH_WEIGHT_DP = 4     # shrink weights, dimensionless


def publish(value, places: int = PUBLISH_MARGIN_DP):
    """Round to the precision the value will be published at, passing None through.

    Use for any number that both feeds a pick decision and lands in the JSON.
    """
    return None if value is None else round(value, places)

# SP+ (Bill Connelly, ESPN) is a neutral-field points-above-average rating, so
# a matchup spread is the rating difference plus home-field advantage.
HOME_FIELD_ADVANTAGE = 2.5      # points; 0 at a neutral site

# Assumed standard error of an SP+ spread, in points.  Only used to present
# SP+ uncertainty -- SP+ is deliberately NOT part of the master pick, because
# it grades around 52-54% against the spread, which is not distinguishable
# from the 52.4% break-even a -110 line demands.  It gets its own tab and its
# own record, and can earn a place in the master later if that record supports
# it.
SP_PLUS_SIGMA = 3.0

# ===========================================================================

# ---------------------------------------------------------------- scope ----

# How many games to model each week, ranked by total open interest across the
# game's strike ladder.  Popularity and tradeability are the same signal: in
# the measured snapshot every one of the top 21 games by OI had an uncertainty
# band <= 1.0 pt, while all four unusable games sat in the bottom eight.
TOP_N = 30

# Kalshi series carrying the two-sided "Team wins by over X.5 points" ladder.
KALSHI_SERIES = "KXNCAAFSPREAD"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# ------------------------------------------------------- strike filtering ----

# A quote wider than this is a market-maker placeholder, not price discovery.
# Measured: median width 6c, p90 80c; one real market quoted 0.08 / 0.88.
MAX_STRIKE_WIDTH = 0.15

# Below this many usable strikes the CDF is too unconstrained to read.
MIN_USABLE_STRIKES = 8

# A strike must exist within this many points of the line being evaluated,
# otherwise the survival function is being read where nothing pins it down.
MAX_STRIKE_DISTANCE = 3.0

# ------------------------------------------------------------- no-play ----

# Band wider than this => no usable signal regardless of where Vegas sits.
# Catches all four junk games in the measured snapshot (4.7 / 12 / 17.6 / 21.1).
MAX_TRADEABLE_BAND = 2.0

# Smallest edge worth calling a pick, in points.
#
# The band test alone is not enough. Within the top 30 the ladders are tight
# enough that bands run 0.1-0.4 pts, so a 0.2pt "edge" clears the band while
# being economically meaningless. Two independent reasons it is noise:
# spreads are quoted in half-points, so an edge under half a point cannot even
# be expressed as a different bet; and near a typical CFB number a point of
# spread is worth roughly 2.4% of win probability, which is about exactly the
# 52.4% break-even a -110 line demands. One point of edge is therefore the
# rough break-even against vig, before any model error at all.
#
# Measured against a live slate this is what separates an honest board from a
# flattering one: on a week where Kalshi and Vegas agreed to within 0.8 pts on
# every Tier A game, the band test alone still produced 16 "picks".
MIN_EDGE_POINTS = 1.0

# Edge magnitude (points) at which a pick stops being a lean.
EDGE_SOLID = 2.0
EDGE_STRONG = 3.5

# ------------------------------------------------------- quality tiers ----

# S tier: the handful of games each week carrying serious money. Measured over
# 239 games, four cleared $500k of combined spread + moneyline dollar volume
# (Oklahoma/Michigan $818k down to Ohio St./Texas $612k). The spread ladder
# alone topped out at $447k, so a ladder-only rule would never fire.
#
# Dollar volume is contracts traded x price, not notional: Kalshi prices each
# contract $0-$1, so counting them at face value would roughly double the
# figure and misrepresent how much money actually changed hands.
S_TIER_DOLLAR_VOLUME = 500_000

# (band_max, min_open_interest, min_fraction_traded)
#
# The labels describe the *market*, not the bet.  Tier measures how precisely
# Kalshi is quoting the game -- how tight the band is and how much money stands
# behind it -- and says nothing about whether the resulting pick is any good.
# Earlier wording ("Tradeable", "Usable") read as a verdict on the wager, which
# is exactly the confusion the page now works to avoid: quality is the market,
# strength (lean / solid / strong) is the call.
TIERS = {
    "A": {"band": 0.5, "oi": 10_000, "traded": 0.60, "label": "Tight market"},
    "B": {"band": 1.0, "oi": 2_000, "traded": 0.30, "label": "Readable market"},
    "C": {"band": 2.0, "oi": 500, "traded": 0.00, "label": "Loose market"},
}
TIER_S = {"label": "Deep market"}
TIER_D = {"label": "Unreadable market"}

# Tiers good enough for the headline record.
HEADLINE_TIERS = ("S", "A")

# Minimum open interest before a game is considered to have any signal at all.
MIN_OPEN_INTEREST = 500

# ----------------------------------------------------------- shrinkage ----

# Assumed standard deviation (points) of the Vegas prior.  Closing CFB spreads
# sit within roughly a point of the true market consensus, so the prior is
# informative but not authoritative -- a Tier A ladder should dominate it.
PRIOR_SIGMA = 1.5

# Floor on the Kalshi band when computing precision weights, so a band of
# exactly 0.0 (possible when bid and ask ladders cross the median in the same
# interval) does not produce an infinite weight.
MIN_BAND_SIGMA = 0.10

# ------------------------------------------------------------ margins ----

# CFB margins are integers; strikes sit on .5 boundaries.  Ties are impossible.
MARGIN_MIN = -70
MARGIN_MAX = 70

# --------------------------------------------------- margin dispersion ----
#
# How far a game's margin scatters around the market's number, as a logistic
# scale.  SD = scale * pi / sqrt(3), so 8.5 is an SD of 15.4 points -- squarely
# in the 13-17 the college game actually produces.
#
# This must NOT be read off the local slope of the Kalshi curve.  Adjacent
# strikes quote on 1c ticks and disagree by several cents for reasons that have
# nothing to do with football: measured across one slate the local slope ran
# from 1.5% to 10.0% of win probability per point, and the steepest implied a
# margin SD of 4 points, which no college football game has ever had.  The
# scale is fitted across every usable strike at once instead, which is stable,
# and then shrunk toward this prior so a thin or one-sided ladder cannot invent
# a shape out of three quotes.
MARGIN_SCALE_PRIOR = 8.5

# The prior counts for this many strikes' worth of information.  Fitted scales
# on a full slate land between 7.6 and 10.9, so the shrinkage is insurance
# against a degenerate ladder rather than the thing setting the answer.
MARGIN_SCALE_PRIOR_STRIKES = 5.0

# Hard sanity bounds: SD 10.9 to 21.8 points.  On a measured slate these never
# bound -- if one starts firing, the fit is wrong, not the game.
MARGIN_SCALE_MIN = 6.0
MARGIN_SCALE_MAX = 12.0

# ------------------------------------------------------------- timing ----

# Picks are due Wednesday night / Thursday morning.  The headline accuracy
# record locks here, because this is the tool as actually used.
DECISION_WEEKDAY = 3      # Thursday (Mon=0)
DECISION_HOUR_ET = 12     # 12:00 ET

# The second lock. Kalshi volume arrives late -- open interest grew a median of
# 80% between the Thursday lock and kickoff, and in one case 4,618% -- so the
# headline record is taken when the market is at its most informative.
FINAL_LOCK_HOURS_BEFORE = 1.0

# ---------------------------------------------------------------- paths ----

DATA_DIR = "docs/data"
HISTORY_DIR = "docs/data/history"
