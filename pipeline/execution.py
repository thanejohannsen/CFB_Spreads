"""Where to place a bet the tool has already decided to make.

The pick is settled before this module runs; the only question left is which
venue leaves the most of it.  That question is worth asking because the edges
are thin -- the master's clear a -110 line by well under two points of
probability -- so the spread between venues is not a rounding error on the
analysis, it is most of it.

Two things make one venue beat another, and only one of them is execution:

  cost of dealing -- what a venue charges over its OWN midpoint.  A -110/-110
                     book charges 2.38 points a side.  Kalshi charges half its
                     bid-ask plus a fee: 2.25 points taking the offer, which is
                     a wash, and -0.06 resting a limit, which is a rebate.
                     Model-free, and the thing this module is for.

  disagreement    -- Kalshi's midpoint against the book's implied even money at
                     the same number.  Measured across a live slate it runs
                     -4.5 to +2.5 points, dwarfing the above.

The second is NOT a saving, and the distinction is load-bearing.  Comparing a
model's cover probability straight to a Kalshi rung's cost silently adds it in,
and on the moneyline lens the result correlates with that lens's own
ML-versus-ladder divergence at r = +0.96 -- it reads the tool's signal back as a
discount and reports up to 10 points of free money.  Disagreement between two
venues is a trade, and it already has its own tab and its own graded record.
So both numbers are computed and they are reported apart.

A Kalshi contract settles at $1, so its all-in cost IS its break-even
probability.  No odds conversion, and the comparison against a sportsbook price
happens in one unit: points of probability, the same unit the card already uses
for cover chance.
"""

from __future__ import annotations

import math
import os
from typing import Optional

from . import config


# --------------------------------------------------------------------------
# Fees
# --------------------------------------------------------------------------

def fee_rate(price: float, maker: bool = False) -> float:
    """Kalshi's fee as a fraction of one contract, unrounded.

    The published formula rounds up to the next cent per ORDER, which makes the
    per-contract cost depend on how many you buy: one contract at 22c is charged
    2c, a thousand are charged 1.2c each.  That rounding is a property of the
    order, not of the price, so every cross-venue comparison here uses the
    unrounded rate and the page says that a single contract rounds up.
    """
    rate = config.KALSHI_FEE_RATE * (config.KALSHI_MAKER_FEE_MULTIPLIER if maker else 1.0)
    return rate * price * (1.0 - price)


def order_fee(price: float, contracts: float, maker: bool = False) -> float:
    """The fee actually charged on an order, in dollars, rounding and all."""
    rate = config.KALSHI_FEE_RATE * (config.KALSHI_MAKER_FEE_MULTIPLIER if maker else 1.0)
    return math.ceil(rate * contracts * price * (1.0 - price) * 100.0) / 100.0


# --------------------------------------------------------------------------
# Break-even
# --------------------------------------------------------------------------

def break_even(cost: float, maker: bool = False) -> float:
    """How often a Kalshi contract bought at `cost` must land to break even.

    It settles at $1, so the price is the probability; the fee is the only thing
    to add.
    """
    return cost + fee_rate(cost, maker)


def american_break_even(price: float) -> Optional[float]:
    """The win rate an American-odds bet needs.  -110 => 0.5238."""
    if price is None or price == 0:
        return None
    if price < 0:
        return -price / (-price + 100.0)
    return 100.0 / (price + 100.0)


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------

def wants_yes(home_strike: bool, side: str) -> bool:
    """Is the pick the rung's YES, or its NO?

    A home-team rung's YES is {margin > x}; an away-team rung's YES is
    {margin < x}, because "away wins by over t" is the same event read from the
    other end.  So the pick is the YES exactly when the two agree.  Getting this
    backwards inverts the answer rather than skewing it -- it prices the other
    team's bet -- so it is one function with one test per combination.
    """
    return home_strike == (side == "home")


def venues(quote: Optional[dict], side: str, p_cover: Optional[float],
           vegas_price: float = None) -> dict:
    """Every way to place this bet, cheapest break-even first.

    `quote` is the rung at the pick's own number -- {bid, ask, bid_size,
    ask_size, home_strike} -- or None when Kalshi does not quote it, which is
    ordinary: the ladder's grid is coarser than the Vegas number on many games.

    Ranking needs no model.  The venue with the lowest break-even leaves the
    most edge whatever the cover probability turns out to be, so `p_cover` only
    scales the numbers; it never reorders them.
    """
    vegas_price = config.ASSUMED_VEGAS_PRICE if vegas_price is None else vegas_price
    book_be = american_break_even(vegas_price)

    rows = [{
        "venue": "book",
        "label": f"Sportsbook {_odds(vegas_price)}",
        "note": "assumed price",          # CFBD publishes the number, not the juice
        "cost": None,
        "break_even": book_be,
        "dealing": None if book_be is None else book_be - 0.5,
        "fillable": True,
    }]

    mid = None
    if quote:
        yes = wants_yes(bool(quote.get("home_strike")), side)
        bid, ask = quote["bid"], quote["ask"]
        bid_size = quote.get("bid_size") or 0.0
        ask_size = quote.get("ask_size") or 0.0
        contract = "YES" if yes else "NO"

        # Buying the YES lifts the offer; buying the NO is matched against the
        # resting yes bids, so each row's depth is the far side's size.
        take_cost, take_size = (ask, ask_size) if yes else (1.0 - bid, bid_size)
        rest_cost = bid if yes else 1.0 - ask

        rows.append({
            "venue": "kalshi_take", "label": f"Kalshi {contract}, take the offer",
            "note": "", "cost": take_cost, "break_even": break_even(take_cost),
            "dealing": None, "contract": contract, "size": take_size,
            # A price with nothing behind it is not a price. Kalshi seeds levels
            # with hundredths of a contract and reports them as top of book.
            "fillable": take_size >= config.MIN_EXECUTABLE_SIZE,
        })
        rows.append({
            "venue": "kalshi_rest", "label": f"Kalshi {contract}, rest a limit",
            "note": "only pays if you get filled", "cost": rest_cost,
            "break_even": break_even(rest_cost, maker=True), "dealing": None,
            "contract": contract, "size": None,
            # Resting posts a new order rather than consuming one, so depth does
            # not gate it -- the risk is a fill, not a price, and the note says so.
            "fillable": True,
        })

        # The picked side's own midpoint: the anchor both Kalshi rows are
        # measured against, so their cost of dealing excludes disagreement.
        mid = (bid + ask) / 2.0
        if not yes:
            mid = 1.0 - mid
        for row in rows[1:]:
            row["dealing"] = row["break_even"] - mid

    for row in rows:
        row["edge"] = (None if p_cover is None or row["break_even"] is None
                       else p_cover - row["break_even"])

    fillable = [r for r in rows if r["fillable"] and r["break_even"] is not None]
    fillable.sort(key=lambda r: r["break_even"])
    # How much the winner actually wins by. Two venues inside EXEC_TIE_POINTS are
    # level: the sportsbook price is assumed, and that assumption is worth far
    # more than such a gap, so naming a winner there is precision the inputs
    # cannot support.
    margin = (fillable[1]["break_even"] - fillable[0]["break_even"]
              if len(fillable) > 1 else None)
    tied = margin is not None and margin < config.EXEC_TIE_POINTS
    for row in rows:
        row["best"] = bool(fillable) and row is fillable[0] and not tied
        row["level"] = (bool(fillable) and not row["best"] and row["fillable"]
                        and row["break_even"] is not None
                        and row["break_even"] - fillable[0]["break_even"]
                        < config.EXEC_TIE_POINTS)

    return {
        "rows": rows,
        "best": fillable[0]["venue"] if fillable and not tied else None,
        "margin": margin,
        "tied": tied,
        "mid": mid,
        # Kalshi's own view against the book's implied even money. Reported so
        # it can be named and set aside, never folded into the saving.
        "disagreement": None if mid is None else 0.5 - mid,
        "quoted": quote is not None,
    }


def _odds(price: float) -> str:
    if price is None:
        return "—"
    return f"+{price:g}" if price > 0 else f"{price:g}"


def quote_at(game: dict, line: Optional[float]) -> Optional[dict]:
    """The published rung at exactly this number.  Mirrors app.js:quoteAt.

    Quotes are [x, bid, ask, bid_size, ask_size] with x signed home-positive,
    the same convention as `strikes`.  Kalshi thresholds are always positive on
    the half-point grid -- 0 of 2,303 rungs across a full slate sat at or below
    zero -- so the sign alone says whose contract a rung is.
    """
    if line is None:
        return None
    for x, bid, ask, bid_size, ask_size in game.get("quotes") or []:
        if abs(x - line) < 1e-9:
            return {"x": x, "bid": bid, "ask": ask, "bid_size": bid_size,
                    "ask_size": ask_size, "home_strike": x > 0}
    return None


def _main() -> int:
    """Emit the venue rows for every stored pick, for tools/conformance.js.

    The page recomputes all of this in the browser; this is the other half of
    that comparison, so the two mirrors cannot drift the way the survival table
    once did.
    """
    import argparse
    import json

    ap = argparse.ArgumentParser(description=_main.__doc__)
    ap.add_argument("data", nargs="?", default=os.path.join(config.DATA_DIR, "current.json"))
    args = ap.parse_args()

    with open(args.data, encoding="utf-8") as fh:
        payload = json.load(fh)

    out = []
    for game in payload.get("games") or []:
        line = game.get("vegas_home_favored_by")
        for lens in ("master", "kalshi_ml"):
            pick = (game.get("picks") or {}).get(lens) or {}
            if not pick.get("side"):
                continue
            out.append({
                "id": game["id"], "lens": lens,
                "venues": venues(quote_at(game, line), pick["side"], pick.get("p_cover")),
            })
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
