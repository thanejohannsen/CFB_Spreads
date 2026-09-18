"""Fetch, model, and write the week's slate to docs/data/.

Run by GitHub Actions on a cron.  Kalshi needs no credentials; CFBD needs a key
for the Vegas baseline but the run still produces useful output without one.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Optional

from . import (cfbd, combine, config, grade_history, kalshi, liquidity,
               moneyline, select_slate, sp_plus, weeks)
from .margin_model import read_market
from .match_games import match_all
from .predict import evaluate

UTC = datetime.timezone.utc


def _slate_date(ladders) -> Optional[datetime.date]:
    """The date most of the slate is played on.

    A CFB week runs Tuesday to Monday, so a Thu/Fri/Sat slate sits inside one
    week and the modal date names it unambiguously."""
    import collections
    dates = [l.game_date for l in ladders if l.game_date]
    if not dates:
        return None
    return datetime.date.fromisoformat(collections.Counter(dates).most_common(1)[0][0])


def _size(contracts: float):
    """Resting size, at the precision anyone reads it at.

    The only question asked of this number is whether it clears one contract, so
    a size in the thousands is published whole -- two decimals on 9776.33 is
    3KB of payload across the slate for digits nothing consults. Below the floor
    the exact figure is kept, because "0.02 contracts" is the evidence that the
    quote is seeded dust rather than a market.
    """
    return (round(contracts, 2) if contracts < config.MIN_EXECUTABLE_SIZE
            else round(contracts))


def _quotes(ladder, read) -> list:
    """Per-rung two-sided quotes within EXEC_QUOTE_RANGE_PTS of the number.

    Unlike `strikes` this deliberately does NOT filter to usable strikes: a rung
    too wide to help fit the curve is still a rung you can trade, and the
    execution box judges it on its own resting size instead.
    """
    out = []
    for st in ladder.strikes:
        x = st.threshold if st.abbrev == ladder.home_abbrev else -st.threshold
        if abs(x - read.implied_margin) > config.EXEC_QUOTE_RANGE_PTS:
            continue
        out.append([round(x, 1), config.publish(st.bid), config.publish(st.ask),
                    _size(st.bid_size), _size(st.ask_size)])
    return sorted(out)


def build(top_n: int = None, verbose: bool = True) -> dict:
    now = datetime.datetime.now(UTC)
    ladders, unparsed = kalshi.fetch_ladders()
    if verbose:
        print(f"kalshi: {len(ladders)} ladders parsed, {len(unparsed)} unparseable")

    slate = select_slate.select(ladders, top_n)
    if not slate:
        raise SystemExit("no ladders returned by Kalshi; refusing to write an empty slate")

    # Keep every game this week has already locked, even if its open interest
    # has since fallen out of the top N. Without this the board silently drops
    # a game the record is still grading, and its T-1h lock freezes at whatever
    # value it held when it fell out -- see select_slate.select.
    slate_day = _slate_date(slate)
    if slate_day is not None:
        pinned = grade_history.locked_ids(
            weeks.week_key(datetime.datetime.combine(
                slate_day, datetime.time(12, 0), tzinfo=UTC)))
        if pinned:
            slate = select_slate.select(ladders, top_n, pinned=pinned)
            if verbose:
                extra = len(slate) - (top_n or config.TOP_N)
                if extra > 0:
                    print(f"slate: pinned {extra} already-locked game(s) back in")

    season = now.year if now.month >= 8 else now.year - 1
    games, lines, unmatched = [], [], []
    sp_ratings: dict = {}
    week = None

    if cfbd.available():
        # Ask which week these games are in, not which week it is now.
        week = cfbd.week_for_date(season, _slate_date(slate))
        if week:
            games, lines, sp_ratings, refreshed = cfbd.cached_week(season, week)
            if verbose:
                source = "refreshed" if refreshed else "from cache"
                print(f"cfbd: week {week}, {len(games)} games, {len(lines)} with lines, "
                      f"{len(sp_ratings)} SP+ ratings ({source})")
    elif verbose:
        print("cfbd: no CFBD_API_KEY set; Kalshi-only mode (manual spreads still work)")

    # Kalshi's own moneyline market on the same games, joined on the shared
    # ticker key. Comparing it to the ladder is a same-exchange consistency
    # check, so a gap cannot be blamed on vig or on two books disagreeing.
    try:
        quotes = moneyline.fetch_quotes()
        if verbose:
            print(f"kalshi moneyline: {len(quotes)} games quoted")
    except Exception as exc:                                # noqa: BLE001
        print(f"moneyline: fetch failed ({exc}); continuing without cross-check", file=sys.stderr)
        quotes = {}

    matches, unmatched = match_all(slate, games, lines)
    matched = sum(1 for m in matches if m.game or m.line)

    # A zero-match means the week is wrong, not that the games are unknown.
    # Calendars shift around byes and Week 0, so try either side before giving
    # up -- this spends calls only on the path that is already broken.
    if cfbd.available() and week and not matched and (games or lines):
        for alt in (week + 1, week - 1):
            if alt < 1:
                continue
            alt_games, alt_lines, alt_sp, _ = cfbd.cached_week(season, alt)
            alt_matches, alt_unmatched = match_all(slate, alt_games, alt_lines)
            if sum(1 for m in alt_matches if m.game or m.line):
                if verbose:
                    print(f"cfbd: week {week} matched nothing; using week {alt} instead")
                week, games, lines = alt, alt_games, alt_lines
                sp_ratings = alt_sp or sp_ratings
                matches, unmatched = alt_matches, alt_unmatched
                matched = sum(1 for m in matches if m.game or m.line)
                break

    sp_index = sp_plus.index_ratings(sp_ratings)
    # With no CFBD data at all, "unmatched" would list the whole slate, which is
    # noise rather than a signal that the alias map needs fixing.
    if not games and not lines:
        unmatched = []

    out_games = []
    for match in matches:
        ladder = match.ladder
        read = read_market(ladder)
        quote = quotes.get(ladder.event_ticker.replace(f"{config.KALSHI_SERIES}-", ""))
        dollar_volume = ladder.dollar_volume + (quote.dollar_volume if quote else 0.0)
        quality = liquidity.grade(read, dollar_volume)

        vegas = match.home_favored_by
        cross = moneyline.cross_check(read, quote)

        # Three independent estimates of the same quantity: how many points the
        # home team gives. Each carries its own precision, in points.
        projection = sp_plus.project(
            sp_index, ladder.home_team, ladder.away_team,
            neutral_site=bool((match.game or {}).get("neutral_site")),
        )
        sig_ladder = combine.ladder_signal(read)
        sig_ml = combine.moneyline_signal(cross)
        sig_sp = combine.sp_plus_signal(projection)

        # The master uses the two Kalshi markets only; SP+ is a lens.
        master = combine.master_composite(sig_ladder, sig_ml)

        # Everything below that feeds a pick decision is quantised to the
        # precision it will be PUBLISHED at, before deciding. docs/app.js re-runs
        # these same rules over the committed JSON and can only read what was
        # written, so deciding from anything sharper is how the board and the
        # record end up disagreeing about the same game. The page is
        # authoritative; see config.publish.
        band = None if read.rejected else (config.publish(read.margin_low),
                                           config.publish(read.margin_high))

        blend = liquidity.shrink(read, vegas) if not read.rejected else liquidity.shrink(read, None)
        # Shrink the composite toward the line, using its own precision. The
        # weight depends only on the composite's sigma, so the page can redo
        # this for any hand-typed line without re-deriving anything.
        master_shrink = config.publish(
            liquidity.weight_for_sigma(master.sigma or 0.0)
            if master.margin is not None else 1.0,
            config.PUBLISH_WEIGHT_DP)
        # Mirror app.js:estimateFor exactly -- it blends the two published values
        # and does not round the product, so neither do we. Identical IEEE-754
        # operations on identical inputs make this bit-for-bit equal, not close.
        master_margin = config.publish(master.margin)
        if master_margin is not None and vegas is not None:
            master_margin = master_shrink * master_margin + (1 - master_shrink) * vegas

        picks = {
            "master": evaluate(master_margin, vegas, ladder.home_team, ladder.away_team,
                               mode="master", read=read, quality=quality, band=band,
                               unavailable=read.rejected or "no Kalshi signal",
                               blend_label=master.describe()).to_dict(),
        }
        picks["master"]["strategy_version"] = config.STRATEGY_VERSION
        for sig in (sig_ladder, sig_ml, sig_sp):
            # Signal.to_dict publishes the margin rounded; a lens must be graded
            # on the number the page shows, same as the master.
            picks[sig.key] = evaluate(config.publish(sig.margin), vegas,
                                      ladder.home_team, ladder.away_team,
                                      mode="lens", read=read,
                                      unavailable=sig.note).to_dict()
        pick = picks["master"]

        # An exact kickoff only exists when CFBD matched the game.  Kalshi's
        # close_time is a settlement deadline that can land days after the game,
        # so fall back to the date encoded in the ticker instead of pretending
        # to know the time.
        kickoff = weeks.parse_ts(
            (match.game or {}).get("start_date") or (match.line or {}).get("start_date")
        )
        game_date = ladder.game_date
        if kickoff is None and game_date:
            # Assume a late-afternoon kickoff purely so week bucketing and the
            # decision deadline land on the right day; the page shows the date only.
            kickoff = weeks.parse_ts(f"{game_date}T19:00:00Z")

        out_games.append({
            "id": ladder.event_ticker,
            "title": ladder.title.replace(": Spread", ""),
            "home_team": ladder.home_team,
            "away_team": ladder.away_team,
            "home_abbrev": ladder.home_abbrev,
            "away_abbrev": ladder.away_abbrev,
            "kickoff": kickoff.isoformat() if kickoff else None,
            "kickoff_exact": bool((match.game or {}).get("start_date")
                                  or (match.line or {}).get("start_date")),
            "game_date": game_date,
            "one_sided": ladder.one_sided,
            "open_interest": round(ladder.total_open_interest),
            "dollar_volume": round(dollar_volume),
            "fraction_traded": round(ladder.fraction_traded, 3),
            "median_width": round(ladder.median_width, 4),
            # Signed usable strike thresholds, so the page can reproduce the
            # "is the curve actually pinned down here?" check exactly rather
            # than approximating it. No quantisation needed: same filter as
            # MarketRead.strike_distance, and Kalshi quotes only on the .5 grid,
            # so rounding to 1dp is lossless and both sides see one list.
            "strikes": [] if read.rejected else sorted(
                round(st.threshold if st.abbrev == ladder.home_abbrev else -st.threshold, 1)
                for st in ladder.usable_strikes()
            ),
            # Executable quotes near the number, so the page can price the bet
            # it just recommended: [x, bid, ask, bid_size, ask_size], x signed
            # home-positive like `strikes`. Sizes travel with the prices because
            # a quote with hundredths of a contract behind it is not a price you
            # can fill, and Kalshi reports those as top of book. Range-limited
            # to keep the payload honest -- the whole ladder for the slate is
            # ~29KB, this is ~8KB, and MAX_STRIKE_DISTANCE already refuses a
            # line more than 3pts from a strike.
            "quotes": [] if read.rejected else _quotes(ladder, read),
            "usable_strikes": read.usable_strikes,
            "total_strikes": read.total_strikes,
            "tier": quality.tier,
            "tier_label": quality.label,
            "tier_reasons": quality.reasons,
            "implied_margin": None if read.rejected else config.publish(read.implied_margin),
            # Logistic scale of the margin distribution. With this and a centre
            # the page computes cover probability in closed form, identically to
            # the pipeline -- no table, no interpolation to disagree about.
            "scale": None if read.rejected else config.publish(read.scale),
            # How far the fitted logistic sits from the strikes at its worst, in
            # probability. Diagnostic: it gates nothing, it says when the two
            # parameters above are a poor description of this particular market.
            "scale_residual": None if read.rejected else config.publish(
                read.scale_residual, config.PUBLISH_WEIGHT_DP),
            # The very values the band test above was decided on.
            "margin_low": None if read.rejected else band[0],
            "margin_high": None if read.rejected else band[1],
            "band": None if read.rejected else round(read.band, 2),
            "blended_margin": None if read.rejected else round(blend.margin, 2),
            "kalshi_weight": round(blend.kalshi_weight, 3),
            "shrink_weight": None if read.rejected else round(
                liquidity.weight_for_sigma(
                    liquidity.sigma_for_band(read.band)), 4),
            "blend_label": blend.describe(),
            "vegas_home_favored_by": vegas,
            "vegas_books": len((match.line or {}).get("books", []) or []),
            "pick": pick,                      # legacy alias for the master pick
            "picks": picks,
            "signals": [sig.to_dict() for sig in (sig_ladder, sig_ml, sig_sp)],
            "master": master.to_dict(),
            # Display and drift-tracking only -- app.js recomputes the estimate
            # from master.margin and master_shrink_weight, exactly as above.
            "master_margin": config.publish(master_margin),
            "master_shrink_weight": master_shrink,
            "sp_plus": None if projection is None else {
                "home_favored_by": round(projection.home_favored_by, 2),
                "home_rating": round(projection.home_rating, 2),
                "away_rating": round(projection.away_rating, 2),
                "home_field": projection.home_field,
                "neutral_site": projection.neutral_site,
            },
            "moneyline": cross.to_dict() if cross else None,
        })

    kickoffs = [weeks.parse_ts(g["kickoff"]) for g in out_games if g.get("kickoff")]
    reference = min(kickoffs) if kickoffs else now

    return {
        "generated_at": now.isoformat(),
        "season": season,
        "cfb_week": week,
        "week_key": weeks.week_key(reference),
        "decision_deadline": weeks.decision_deadline(reference).isoformat(),
        "cfbd_available": cfbd.available(),
        "sp_plus_available": bool(sp_index),
        "strategy_version": config.STRATEGY_VERSION,
        "top_n": top_n or config.TOP_N,
        "slate_size": len(out_games),
        "unmatched": unmatched,
        "matched_games": matched,
        "slate_date": (_slate_date(slate).isoformat() if _slate_date(slate) else None),
        "unparsed_events": unparsed,
        "games": out_games,
    }


def write(payload: dict, data_dir: str = None) -> str:
    data_dir = data_dir or config.DATA_DIR
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "current.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), sort_keys=False)
        fh.write("\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="model the slate but write nothing")
    ap.add_argument("--top-n", type=int, default=None)
    args = ap.parse_args()

    payload = build(top_n=args.top_n)

    tiers: dict[str, int] = {}
    for g in payload["games"]:
        tiers[g["tier"]] = tiers.get(g["tier"], 0) + 1
    print(f"\nslate: {payload['slate_size']} games  tiers: {dict(sorted(tiers.items()))}")
    for lens in ("master", "kalshi_spread", "kalshi_ml", "sp_plus"):
        n = sum(1 for g in payload["games"] if (g["picks"].get(lens) or {}).get("side"))
        print(f"  {lens:<14} {n:>3} picks")
    if payload["unmatched"]:
        print(f"unmatched against CFBD ({len(payload['unmatched'])}): "
              f"{', '.join(payload['unmatched'][:5])}")

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    print("wrote", write(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
