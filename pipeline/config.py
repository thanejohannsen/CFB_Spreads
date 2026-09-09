"""Central configuration for the CFB spreads pipeline.

Every threshold here was calibrated against real Kalshi data measured on a
Wednesday afternoon against the following Saturday's slate -- i.e. at the
decision point, not at the kickoff liquidity peak.  See README for the
measurements behind each number.
"""

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

# ------------------------------------------------------- quality tiers ----
# (band_max, min_open_interest, min_fraction_traded)
TIERS = {
    "A": {"band": 0.5, "oi": 10_000, "traded": 0.60, "label": "Tradeable"},
    "B": {"band": 1.0, "oi": 2_000, "traded": 0.30, "label": "Usable"},
    "C": {"band": 2.0, "oi": 500, "traded": 0.00, "label": "Thin"},
}
TIER_D = {"label": "No signal"}

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

# ------------------------------------------------------------- timing ----

# Picks are due Wednesday night / Thursday morning.  The headline accuracy
# record locks here, because this is the tool as actually used.
DECISION_WEEKDAY = 3      # Thursday (Mon=0)
DECISION_HOUR_ET = 12     # 12:00 ET

# ---------------------------------------------------------------- paths ----

DATA_DIR = "docs/data"
HISTORY_DIR = "docs/data/history"
