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

## Four tabs, four records

**Master** is the tool's official pick. It combines the two Kalshi signals by **measured precision**,
per game — each market's own bid/ask converted onto the spread scale, so the sharper one carries the
blend:

```
σ_ladder = ladder band / 2
σ_ml     = (spread implied by the ML's ask − by its bid) / 2
combo    = precision-weighted average,  w = 1/σ²
```

Which one wins depends on the game, and that is the point. On **Arizona St. vs Texas A&M** the ladder
pins the spread to ±0.15 pts while the moneyline manages only ±0.42, so the ladder takes 88% and an
apparent 1-point conflict collapses to a 0.2-point one. On a near pick'em like **Mississippi St. vs
Minnesota** it inverts and the moneyline takes 73%. The reason is structural: the moneyline is quoted
at margin zero, so on a lopsided game its estimate has to be dragged along the curve to reach the
spread, amplifying its error, while the ladder has real traded strikes sitting at the number itself.
Measured across a slate the ladder's share ranged 26%–99%.

The combined uncertainty is **floored at the sharper input** rather than using `1/√(Σw)`. Inverse-
variance weighting assumes independent errors; these are the same exchange and largely the same
traders, so the textbook formula would report the blend as sharper than either input. Averaging the
estimates is right; claiming the variance shrank is not.

The other three tabs are **lenses** — one signal each, judged on its own:

| Tab | Estimate |
|---|---|
| Kalshi Spread vs Vegas Spread | the ladder's median |
| Kalshi ML vs Spread | the moneyline converted to a spread |
| SP+ vs Vegas Spread | (SP+ home − SP+ away) + 2.5 home field |

Lenses use a deliberately looser rule: pick whenever the edge clears a point, with no band test, no
tier gate and no shrinkage toward the line. A lens exists to measure whether its signal carries
information at all, so filtering it through the master's safety rules — or pulling it toward the very
line it is being graded against — would destroy the thing being measured.

**Each tab keeps its own record.** The three lens definitions never change, so those rows stay
comparable all season however the master is re-tuned, and after a few weeks they show which signal is
actually carrying the result. Only the master can shift meaning underneath you, so each locked master
pick carries a `strategy_version` stamp; if the formula ever changes mid-season the row can be split
at that point instead of silently blending two rule sets.

SP+ is **not** part of the master. It grades around 52–54% against the spread, which is not
distinguishable from the 52.4% break-even a −110 line demands, so letting it move a number backed by
hundreds of thousands of contracts would add noise. It keeps its own tab and record and can be
promoted later if that record earns it.

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
3. Enable Pages: Settings → Pages → Source → **Deploy from a branch**, branch
   `claude/optimistic-edison-hkp7v2`, folder **`/docs`**.

The site is published by GitHub's own branch builder, so there is no Pages workflow in this repo —
every commit the updater pushes republishes the site automatically. `docs/.nojekyll` turns off Jekyll
processing, which a branch deploy would otherwise apply.

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

### Scheduled updates

`.github/workflows/update-data.yml` refreshes the data on a cron and commits it back to the branch.
GitHub only runs scheduled workflows on a repository's **default branch**, so the branch the cron
runs on, the branch it commits to, and the branch Pages serves all have to be the same one — they
are. If you ever rename or change the default branch, repoint Settings → Pages to match, or the site
will quietly freeze at its last update while the workflow keeps succeeding elsewhere.

CI deliberately skips commits that touch only `docs/data`, so the automated refreshes do not each
trigger a test run.

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
