# CFB Spreads

A GitHub Pages site that reads Kalshi's prediction markets and says which side of a college
football spread it thinks covers — and, just as often, that it has no idea.

Ten picks are due every Wednesday night / Thursday morning. This tool models the 30 most popular
Kalshi games each week, compares the market's implied spread against the Vegas number, and keeps a
graded record of how it has actually done.

---

## The idea

Kalshi quotes a **two-sided ladder** of "team wins by over X.5 points" markets on each game:

```
Boston College wins by over 20.5 points   bid 0.09 / ask 0.14
Boston College wins by over  2.5 points   bid 0.54 / ask 0.56
Rutgers        wins by over  1.5 points   bid 0.38 / ask 0.41
```

Every price is a probability, so the ladder **is a margin-of-victory distribution**. The chance a
team covers any number is then read straight off the market — `P(LSU covers -10.5)` is just
`P(margin > 10.5)` — instead of coming from a power rating someone invented.

## The part that actually matters: uncertainty

Most Kalshi college football markets are thin. Measured across all 2,478 open spread markets:

- **64% have zero open interest and have never traded.** Their quoted price is a market-maker
  placeholder, not price discovery.
- Bid-ask width runs a median of 6c but a p90 of **80c**. One live market quoted `0.08 / 0.88`.

So the implied spread is derived **twice — once using only bids, once using only asks**. The gap
between them is the market's own uncertainty:

| Game | Median width | Open interest | Implied spread, bids → asks | Band |
|---|---|---|---|---|
| Arizona St. / Texas A&M | 3.0c | 23,091 | TXAM +14.1 → +14.2 | **0.1 pt** |
| Old Dominion / Virginia Tech | 3.0c | 13,475 | VT +19.2 → +20.1 | 0.9 pt |
| Wofford / Kent St. | 8.0c | 1,864 | KENT +2.0 → **−10.0** | 12 pt |
| Southern Utah / Colorado St. | 8.0c | 167 | CSU +0.0 → **+21.2** | 21 pt |

This beats filtering on bid-ask width because it measures uncertainty **at the median crossing**,
which is where the number is actually read. Wofford/Kent St. has a respectable 8c median width but
12 points of slop, because the *wide* strikes sit right at the crossing — a width filter waves it
through.

**When the Vegas line falls inside that band, the tool says _No play_.** The apparent edge is
smaller than the noise it was measured against. A band over 2 points means no pick at all.

## Does the Wednesday deadline hurt?

Less than you would think, because liquidity concentrates in exactly the games worth picking.
Measured on a Wednesday afternoon against the following Saturday:

| Game | Open interest | Band | % strikes traded |
|---|---|---|---|
| Oklahoma vs Michigan | 392,557 | 0.3 pt | 96% |
| Missouri vs Kansas | 135,437 | 0.2 pt | 94% |
| Ohio St. vs Texas | 41,436 | 0.3 pt | 85% |
| … | | | |
| Southern Utah vs Colorado St. | 167 | 21.1 pt | 12% |

**All 21 of the highest-interest games already had a band ≤ 1.0 pt on Wednesday** (median 0.39).
Scoping to the top 30 by open interest is self-reinforcing: popularity and tradeability are the same
signal, so the junk is excluded by construction rather than by filtering.

The record is still locked at **Thursday noon ET** — the moment you actually decide — and a second
snapshot is taken at kickoff purely to measure drift. Over a season that answers "does a Wednesday
read hold up?" with evidence instead of assumption.

## Moneyline cross-check

Ties are impossible, so the ladder already contains a win probability: `P(home wins) = S(0)`. Kalshi
also runs a **separate moneyline market** on the same game, and every spread event has a matching one
on an identical ticker key.

Comparing them is a stronger signal than Kalshi-vs-Vegas — same exchange, same participants, so a gap
cannot be blamed on vig or two books disagreeing. It is also asymmetric in a useful way: the
moneyline markets carry far more open interest (over a million contracts against tens of thousands on
the same game's ladder), so when they disagree it is usually the thinner ladder that is wrong.

The conversion runs both ways. Spread → moneyline is `S(0)`, shown as a probability and as American
odds. Moneyline → spread shifts the game's *own* margin distribution until `P(margin > 0)` matches
the quoted probability, then reads the new median — which respects how that specific matchup's
margins are dispersed rather than leaning on a generic win-probability table.

A conversion landing outside the range the strikes actually cover is reported as extrapolation, not
as a tradeable disagreement: near the tails the slope of margin against probability is steep enough
that a fraction of a point of noise turns into several points of apparent edge.

---

## Setup

1. Get a free API key at [collegefootballdata.com/key](https://collegefootballdata.com/key)
   (1,000 calls/month, which is ample — see caching below).
2. Add it as a repository secret named **`CFBD_API_KEY`**
   (Settings → Secrets and variables → Actions). It is only ever read inside GitHub Actions and is
   never shipped to the browser.
3. Enable Pages: Settings → Pages → Source → **GitHub Actions**.

Without a key everything still runs — Kalshi needs no authentication — you just enter the ten spreads
by hand on the page instead of getting them automatically.

## Running locally

```bash
python -m unittest discover -s pipeline/tests -t .   # 46 tests, no dependencies
python -m pipeline.build_predictions --dry-run       # model the slate, write nothing
python -m pipeline.update                            # write docs/data/*.json
cd docs && python -m http.server 8000                # then open localhost:8000
```

The pipeline is **stdlib-only** — no numpy, no scipy, no requests. Isotonic regression (PAVA) and
monotone cubic interpolation are implemented directly, so a scheduled run cannot break on an upstream
release and there is nothing to install.

## Layout

```
pipeline/
  margin_model.py     ladder -> survival function -> implied spread, P(cover), uncertainty band
  liquidity.py        quality tiers and precision-weighted shrinkage toward a prior
  moneyline.py        cross-check against Kalshi's own moneyline market
  select_slate.py     rank by open interest, lock the week's top 30
  match_games.py      Kalshi <-> CFBD name and date matching
  predict.py          line + market read -> pick, or an explicit refusal
  grade_history.py    dual decision/closing locks, grading, the scoreboard
docs/                 the Pages site (vanilla HTML/CSS/JS, no build step)
  data/current.json   this week's slate, with a dense survival table per game
  data/history/       immutable weekly snapshots, never rewritten
```

The heavy math stays in Python. Each game ships a dense survival table on the half-integer grid, so
typing a spread on the page is a table lookup rather than a re-fit. `docs/app.js` mirrors
`pipeline/predict.py` so a hand-entered line is judged by the same rules that grade the record;
the rule set is kept small so that stays true.

### API budget

Kalshi is unauthenticated and returns the whole slate in one request, so a refresh costs about two
calls and can run as often as it likes. CFBD responses are cached in `docs/data/cfbd_snapshot.json`
and refreshed every 8 hours, which holds the monthly count near 180 against the free tier's 1,000
while leaving the Kalshi cadence untouched.

---

## What this is not

Kalshi and Vegas are both reasonably efficient markets. Real edges are small and frequently explained
by liquidity rather than insight. This is a screening tool: it shows where two markets disagree and
how much that disagreement is worth trusting, and it declines to pick when it cannot support one.

Some weeks it will not find ten confident picks. That is the tool working, not failing. The graded
record on the page — locked before kickoff, never revised, with games that had no line or no pick
excluded from the denominator rather than quietly counted — is the honest test of whether any of it
is worth anything.
