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

from . import cfbd, config, kalshi, liquidity, moneyline, select_slate, weeks
from .margin_model import read_market
from .match_games import match_all
from .predict import evaluate

UTC = datetime.timezone.utc


def build(top_n: int = None, verbose: bool = True) -> dict:
    now = datetime.datetime.now(UTC)
    ladders, unparsed = kalshi.fetch_ladders()
    if verbose:
        print(f"kalshi: {len(ladders)} ladders parsed, {len(unparsed)} unparseable")

    slate = select_slate.select(ladders, top_n)
    if not slate:
        raise SystemExit("no ladders returned by Kalshi; refusing to write an empty slate")

    season = now.year if now.month >= 8 else now.year - 1
    games, lines, unmatched = [], [], []
    week = None

    if cfbd.available():
        week = cfbd.current_week(season)
        if week:
            games, lines, refreshed = cfbd.cached_week(season, week)
            if verbose:
                source = "refreshed" if refreshed else "from cache"
                print(f"cfbd: week {week}, {len(games)} games, {len(lines)} with lines ({source})")
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
    # With no CFBD data at all, "unmatched" would list the whole slate, which is
    # noise rather than a signal that the alias map needs fixing.
    if not games and not lines:
        unmatched = []

    out_games = []
    for match in matches:
        ladder = match.ladder
        read = read_market(ladder)
        quality = liquidity.grade(read)

        vegas = match.home_favored_by
        blend = liquidity.shrink(read, vegas) if not read.rejected else liquidity.shrink(read, None)
        pick = evaluate(read, quality, blend, vegas, ladder.home_team, ladder.away_team)

        key = ladder.event_ticker.replace(f"{config.KALSHI_SERIES}-", "")
        cross = moneyline.cross_check(read, quotes.get(key))

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
            "fraction_traded": round(ladder.fraction_traded, 3),
            "median_width": round(ladder.median_width, 4),
            # Signed usable strike thresholds, so the page can reproduce the
            # "is the curve actually pinned down here?" check exactly rather
            # than approximating it.
            "strikes": [] if read.rejected else sorted(
                round(st.threshold if st.abbrev == ladder.home_abbrev else -st.threshold, 1)
                for st in ladder.usable_strikes()
            ),
            "usable_strikes": read.usable_strikes,
            "total_strikes": read.total_strikes,
            "tier": quality.tier,
            "tier_label": quality.label,
            "tier_reasons": quality.reasons,
            "implied_margin": None if read.rejected else round(read.implied_margin, 2),
            "margin_low": None if read.rejected else round(read.margin_low, 2),
            "margin_high": None if read.rejected else round(read.margin_high, 2),
            "band": None if read.rejected else round(read.band, 2),
            "blended_margin": None if read.rejected else round(blend.margin, 2),
            "kalshi_weight": round(blend.kalshi_weight, 3),
            "shrink_weight": None if read.rejected else round(
                liquidity.weight_for_band(read.band), 4),
            "blend_label": blend.describe(),
            "vegas_home_favored_by": vegas,
            "vegas_books": len((match.line or {}).get("books", []) or []),
            "survival": {
                # table[i] = P(home margin > first + i), on the half-integer
                # grid where Kalshi actually quotes.
                "first": config.MARGIN_MIN + 0.5,
                "step": 1.0,
                "table": [] if read.rejected else read.table(),
            },
            "pick": pick.to_dict(),
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
        "top_n": top_n or config.TOP_N,
        "slate_size": len(out_games),
        "unmatched": unmatched,
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
    picks = sum(1 for g in payload["games"] if g["pick"]["side"])
    print(f"\nslate: {payload['slate_size']} games  tiers: {dict(sorted(tiers.items()))}  "
          f"actionable picks: {picks}")
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
