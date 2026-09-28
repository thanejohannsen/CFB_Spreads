"""Lock predictions before they can be revised, then grade them.

Two locks are recorded per game, each carrying every pick the Master tab makes
and differing only in when it stops moving:

  decision -- the last snapshot before Thursday noon ET.  The tool as actually
              used: picks are due Wednesday night / Thursday morning.
  final    -- the last snapshot before kickoff minus an hour, where Kalshi's
              volume peaks.  The headline record, and the measure of the method
              at its sharpest.

Until its moment arrives a lock is rewritten on every run, so it holds the live
board; the record shows it as such, marked live and dated by the moment it will
freeze.  Once the moment passes it is never rewritten again.  An accuracy number
you can revise after the fact is worth nothing, and a record you cannot
reconcile against the board is worth about as little.
"""

from __future__ import annotations

import datetime
import json
import math
import os
from typing import Optional

from . import cfbd, config, kalshi, weeks

UTC = datetime.timezone.utc

EDGE_BUCKETS = [("<1pt", 0.0, 1.0), ("1-3pts", 1.0, 3.0), (">3pts", 3.0, float("inf"))]

# One record per tab. The three lens definitions never change, so those records
# stay comparable all season no matter how the master is re-tuned.
def _lock(entry: dict, name: str) -> Optional[dict]:
    """Fetch a lock, tolerating the pre-T-1h schema.

    The second lock used to sit at kickoff and was called `closing`. Reading it
    as `final` keeps already-locked weeks in the record rather than dropping
    them when the schema moved."""
    if name == "final" and not entry.get("final"):
        return entry.get("closing")
    return entry.get(name)


LENSES = [
    ("master", "Master"),
    ("kalshi_spread", "Kalshi Spread vs Vegas Spread"),
    ("kalshi_ml", "Kalshi ML vs Spread"),
    ("sp_plus", "SP+ vs Vegas Spread"),
]

# Whose picks are worth holding a game in the slate for -- see locked_ids.
#
# SP+ is left out on the evidence, not for convenience. It is a near-static
# power rating, so its pick barely moves between the two locks: measured across
# the stored weeks it changed on 24% of its 76 picked games, against 74% for the
# moneyline and 100% for the master and the ladder. Pinning exists to let a pick
# be RE-EVALUATED and dropped, which is worth little on a signal that does not
# move -- and SP+ alone accounted for 17 of the 19 games the old rule dragged
# into the slate beyond the top 30.
PINNING_LENSES = ("master", "kalshi_spread", "kalshi_ml")

# Every record the page can show, as (key, label, lock, lens, tier filter).
#
# Both master records carry every pick the Master tab makes, and differ only in
# the clock they freeze on: the headline at T-1h, the Thursday row at noon ET.
#
# Neither filters on tier. A record the board cannot be reconciled against is
# the one thing worse than a flattering record: the headline used to keep S/A
# markets only, so a board showing six master picks sat above a record holding
# none of them, and no amount of reading the page explained the gap. The master
# already refuses a D-grade market and caps a C-grade one at a lean, so the
# picks it does make are the picks it stands behind -- all of them, at both
# clocks.
#
# There was briefly an S/A-filtered Thursday row as a control, to separate the
# effect of the clock from the effect of the tier filter. It was dropped because
# it could never hold data: across the stored weeks the master picked 0 of 36
# A-tier games, since a tight band is exactly when the line sits inside it and
# the no-play rule reads no edge. A control with nothing in it is not a control.
#
# The moneyline lens is graded on both clocks too, and it is the only lens that
# is. The master earns a second clock because the question "does a Wednesday
# read hold up?" deserves evidence rather than assumption, and the moneyline is
# the signal most likely to answer it differently: it is the one carrying most
# of the master's weight on a tight game, and Kalshi's volume arrives late. The
# pair already holds data and is not a duplicate -- across the stored weeks the
# T-1h lock picked 32 games to Thursday's 23, while the two chose opposite sides
# exactly once. Both samples are far too small to read anything into the split
# yet, which is the point of starting to keep it.
#
# Nothing had to be computed for this. apply_results already writes
# `final_correct` for every lens and _snapshot freezes all four picks into both
# locks, so the row was always there to list. The other two lenses stay on one
# clock until this pair shows the second is worth the column.
# The sixth field scopes a record to a set of STRATEGY_VERSIONs, the same shape
# as the tier filter: None counts everything.
#
# It is NOT applied to every record, because not every pick changed when the
# formula did. v2 refit the logistic scale, and only signals that read that
# scale moved with it:
#
#   moneyline.cross_check   reads read.scale at five call sites    -> moved
#   combine.master_composite  inherits the moneyline's sigma       -> moved
#   combine.ladder_signal   reads the ladder MEDIAN and the band   -> unchanged
#   combine.sp_plus_signal  reads static ratings and a fixed sigma -> unchanged
#
# So a v1-stamped SP+ pick is what v2 would have produced anyway. Filtering it
# would have thrown away 36 of its 60 graded games for nothing -- and SP+ is
# the only record picking fast enough to reach a verdict this season.
CURRENT = (config.STRATEGY_VERSION,)

RECORDS = [
    ("headline", "Final (T-1h)", "final", "master", None, CURRENT),
    ("thursday_all", "Thursday noon", "decision", "master", None, CURRENT),
    # Keyed `lens_kalshi_ml` still, and deliberately: the lens keys are meant to
    # outlive relabelling. Only the label gains its clock, now that the name
    # alone no longer identifies one row.
    ("lens_kalshi_ml", "Kalshi ML vs Spread - Thursday noon", "decision", "kalshi_ml",
     None, CURRENT),
    ("lens_kalshi_ml_final", "Kalshi ML vs Spread - Final (T-1h)", "final", "kalshi_ml",
     None, CURRENT),
] + [
    (f"lens_{key}", label, "decision", key, None, None)
    for key, label in LENSES if key not in ("master", "kalshi_ml")
]


def _version(snapshot: Optional[dict]) -> Optional[str]:
    """Which formula produced this snapshot.

    Only the master pick carries the stamp -- build_predictions sets it on
    `picks["master"]` alone, because the three lens definitions never change.
    But every snapshot has a master pick dict whether or not it picked a side,
    so the snapshot's version is readable from there for all four lenses. That
    is what lets a moneyline row be scoped without stamping it twice.

    None means the snapshot predates versioning, which is certainly not the
    current formula; see _in_scope.
    """
    return ((_picks(snapshot) or {}).get("master") or {}).get("strategy_version")


def _picks(snapshot: Optional[dict]) -> dict:
    """All four picks from a snapshot, tolerating the pre-lens schema.

    Snapshots written before the lens tabs existed carry a single `pick`, which
    was the master pick. Reading it as such keeps those locked weeks in the
    record instead of discarding or rewriting them."""
    if not snapshot:
        return {}
    if snapshot.get("picks"):
        return snapshot["picks"]
    legacy = snapshot.get("pick")
    return {"master": legacy} if legacy else {}


# --------------------------------------------------------------------------
# Snapshot storage
# --------------------------------------------------------------------------

def history_path(week_key: str, history_dir: str = None) -> str:
    return os.path.join(history_dir or config.HISTORY_DIR, f"{week_key}.json")


def load_week(week_key: str, history_dir: str = None) -> dict:
    path = history_path(week_key, history_dir)
    if not os.path.exists(path):
        return {"week_key": week_key, "games": {}}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_week(week: dict, history_dir: str = None) -> str:
    history_dir = history_dir or config.HISTORY_DIR
    os.makedirs(history_dir, exist_ok=True)
    path = history_path(week["week_key"], history_dir)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(week, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return path


def _snapshot(game: dict, at: datetime.datetime,
              kickoff: Optional[datetime.datetime] = None) -> dict:
    """The fields worth freezing; the full survival table is not one of them."""
    minutes = None
    if kickoff is not None:
        minutes = round((kickoff - at).total_seconds() / 60)
    return {
        "at": at.isoformat(),
        # How stale this lock is. A missed cron run means the snapshot may be
        # much older than intended, and without recording the gap a stale lock
        # is indistinguishable from a fresh one -- the record would quietly
        # overstate what the market knew.
        "minutes_before_kickoff": minutes,
        "dollar_volume": game.get("dollar_volume"),
        "implied_margin": game.get("implied_margin"),
        "blended_margin": game.get("blended_margin"),
        "band": game.get("band"),
        "margin_low": game.get("margin_low"),
        "margin_high": game.get("margin_high"),
        "tier": game.get("tier"),
        "open_interest": game.get("open_interest"),
        "fraction_traded": game.get("fraction_traded"),
        "kalshi_weight": game.get("kalshi_weight"),
        "vegas_home_favored_by": game.get("vegas_home_favored_by"),
        "master_margin": game.get("master_margin"),
        "picks": game.get("picks"),
        "pick": game.get("pick"),          # legacy alias for the master pick
    }


def _keeps_its_line(old: Optional[dict], new: dict) -> bool:
    """Would this refresh blank out a line the existing lock already had?

    A run without CFBD_API_KEY still reaches Kalshi, so it produces a complete
    looking slate with `vegas_home_favored_by` None on every game -- and every
    pick a no-play, since there is no line to disagree with. Rewriting a lock
    with that erases the real line and the real picks behind it, and the week
    then grades as though the tool never had an opinion. Observed locally: one
    keyless run took a stored week from 30 priced locks to 0.

    Refreshing only the Kalshi fields is not a fix either, because the picks in
    the same snapshot were computed against the missing line. The whole lock
    has to stay.
    """
    if not old:
        return True
    return not (old.get("vegas_home_favored_by") is not None
                and new.get("vegas_home_favored_by") is None)


def locked_ids(week_key: str, history_dir: str = None,
               now: datetime.datetime = None) -> set:
    """Games worth holding in the slate, for build_predictions to pin.

    One test: does any lock on this game hold a pick one of PINNING_LENSES
    stands behind?  That covers both reasons a game has to stay in the payload,
    and they are different reasons:

      * a lock still OPEN needs the game evaluated every run so the pick can be
        DROPPED when the edge goes.  record() only ever sees games in the
        payload, so a picked game that slips out of the top N stops being
        re-evaluated and its lock freezes holding a pick the tool no longer
        makes.  That is how Michigan St. vs Notre Dame came to be credited to
        Thursday noon with a snapshot taken twelve hours earlier.
      * a lock already FIRED keeps the game in the payload so the board can
        still show it, and a board that has dropped a game the record grades
        cannot be reconciled against it. That window is narrower than it looks:
        the T-1h lock fires an hour before kickoff, and the board hides a game
        once it starts, so this is really about that last hour.

    A game holding no pick is pinned by neither.  If it is inside the top N it
    is in the payload anyway; if it is not, it is a game the tool has no opinion
    on, and the record needs nothing from it.

    This used to pin any game with a lock at all until its T-1h passed, which
    before Saturday is every game that has touched the top N all week -- 49
    against a TOP_N of 30, so the board filled with markets the tool does not
    claim to cover and TOP_N stopped meaning anything.  Keeping a game because
    it was once seen is not the same as keeping one because it is picked.

    `now` is accepted and ignored.  The old rule turned on whether T-1h had
    passed; this one asks only whether a pick exists, which is the same answer
    at every moment -- so the parameter stays for its callers and tests rather
    than because the answer needs it.
    """
    del now
    out = set()
    for gid, entry in (load_week(week_key, history_dir).get("games") or {}).items():
        for name in ("decision", "final"):
            picks = _picks(_lock(entry, name)) or {}
            if any((picks.get(key) or {}).get("side") for key in PINNING_LENSES):
                out.add(gid)
                break
    return out


def record(payload: dict, history_dir: str = None, now: datetime.datetime = None) -> str:
    """Fold the current slate into this week's history file.

    A lock is refreshed only while its moment is still ahead; once passed it is
    frozen for good.  That yields exactly "the last snapshot before the
    deadline" without needing to guess which run will be the last.
    """
    now = now or datetime.datetime.now(UTC)
    skipped: list[str] = []
    week = load_week(payload["week_key"], history_dir)
    week.setdefault("season", payload.get("season"))
    week.setdefault("games", {})

    for game in payload["games"]:
        entry = week["games"].setdefault(game["id"], {
            "title": game["title"],
            "home_team": game["home_team"],
            "away_team": game["away_team"],
            "kickoff": game.get("kickoff"),
        })
        entry["kickoff"] = game.get("kickoff") or entry.get("kickoff")

        kickoff = weeks.parse_ts(entry.get("kickoff"))
        if kickoff is None:
            continue

        for name, moment in (("decision", weeks.decision_deadline(kickoff)),
                             ("final", weeks.final_lock(kickoff))):
            if now >= moment:
                continue                       # frozen; never rewritten
            fresh = _snapshot(game, now, kickoff)
            if _keeps_its_line(entry.get(name), fresh):
                entry[name] = fresh
            else:
                skipped.append(f"{game['title']} ({name})")

    if skipped:
        print(f"history: kept {len(skipped)} lock(s) rather than blank their Vegas line "
              f"-- this run had no line for them (missing CFBD_API_KEY?)")
        for s_ in skipped[:5]:
            print(f"  kept {s_}")

    return save_week(week, history_dir)


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------

def _decide(snapshot: Optional[dict], lens: str, margin: Optional[float],
            bracket: Optional[tuple] = None) -> Optional[bool]:
    """Did this lens's pick cover?

    Two kinds of evidence reduce to one answer. A final score names the margin
    outright. A settled Kalshi ladder names a range it fell inside, which still
    decides any pick whose number sits outside that range -- and most do, since
    the rungs are a point and a half apart either side of the result.

    None when there was no pick, when the game pushed, or when the range
    straddles the number. The last is the one worth being strict about: a
    bracket that does not reach the line is missing evidence, not a near miss,
    and guessing from its midpoint would put invented results into a record
    whose whole claim is that it cannot be revised after the fact.
    """
    pick = (_picks(snapshot) or {}).get(lens) or {}
    side, line = pick.get("side"), pick.get("line")
    if side is None or line is None:
        return None

    if margin is not None:
        if abs(margin - line) < 1e-9:
            return None                               # push
        home_covered = margin > line
    elif bracket is not None:
        low, high = bracket
        if low >= line:
            home_covered = True                       # margin > low >= line
        elif high <= line:
            home_covered = False                      # margin < high <= line
        else:
            return None
    else:
        return None

    return home_covered if side == "home" else not home_covered


def _pinned(bracket: Optional[tuple]) -> Optional[float]:
    """The exact margin, when exactly one whole number fits the bracket.

    Worth extracting because a margin is what the page shows and what the drift
    numbers are computed from; a bracket one point wide gives it for free, which
    on a full slate is about four games in ten.
    """
    if not bracket:
        return None
    low, high = bracket
    if math.isinf(low) or math.isinf(high):
        return None
    fits = [n for n in range(math.floor(low) + 1, math.ceil(high)) if low < n < high]
    return float(fits[0]) if len(fits) == 1 else None


def _finite(value: Optional[float]) -> Optional[float]:
    """An open end of a bracket is an infinity, and JSON has no word for one."""
    if value is None or math.isinf(value):
        return None
    return value


def apply_results(week: dict, results: dict[str, float],
                  brackets: dict[str, tuple] = None) -> dict:
    """Attach what is known about each final margin, and grade both locks.

    `results` holds exact margins keyed by event id; `brackets` holds (low,
    high) ranges from settled Kalshi ladders for the games `results` could not
    reach. A game with neither is left untouched -- an entry carrying no
    `result` is what every caller reads as "not played yet", and writing an
    empty one would take those picks out of the pending column without putting
    them in the record.
    """
    brackets = brackets or {}
    for game_id, entry in week.get("games", {}).items():
        margin = results.get(game_id)
        bracket = brackets.get(game_id) if margin is None else None
        if margin is None:
            margin = _pinned(bracket)
        if margin is None and bracket is None:
            continue
        entry["result"] = {
            "home_margin": margin,
            # Kept even when the margin is known, so a settlement-graded game
            # can be told from a score-graded one long after the fact.
            "margin_low": _finite(bracket[0]) if bracket else None,
            "margin_high": _finite(bracket[1]) if bracket else None,
            "source": "kalshi" if bracket else "cfbd",
            "decision_correct": _decide(_lock(entry, "decision"), "master", margin, bracket),
            "final_correct": _decide(_lock(entry, "final"), "master", margin, bracket),
            "lenses": {
                key: {
                    "decision_correct": _decide(_lock(entry, "decision"), key, margin, bracket),
                    "final_correct": _decide(_lock(entry, "final"), key, margin, bracket),
                }
                for key, _ in LENSES
            },
        }
    return week


def _finals_for(week: dict, season: int, saturday: datetime.date) -> tuple:
    """Exact margins from CFBD's final scores, keyed by stored event id.

    Returns (results, note).  The note says how the week number was arrived at,
    because that is the thing that silently went wrong: a calendar holding only
    two weeks answered "week 3" for every date in October with no way to tell
    the answer from a guess.
    """
    if not cfbd.available():
        return {}, "no CFBD key"

    cfb_week, resolved = cfbd.week_for_date_checked(season, saturday)
    if cfb_week is None:
        return {}, "no calendar"

    weeks_to_try = [cfb_week]
    if not resolved:
        # The calendar did not reach this date, so the number is the nearest
        # week rather than the right one.  The board already retries its
        # neighbours for exactly this reason; grading did not, and that is how
        # a slate of 85 games came to be asked about the wrong week and quietly
        # matched nothing.
        weeks_to_try += [cfb_week + 1, cfb_week - 1]

    from .match_games import normalize
    for candidate in weeks_to_try:
        if candidate < 1:
            continue
        finals = {}
        for g in cfbd.cached_finals(season, candidate):
            if g.get("home_margin") is None or not g.get("completed"):
                continue
            finals[frozenset((normalize(g["home_team"]), normalize(g["away_team"])))] = g

        results = {}
        for game_id, entry in week["games"].items():
            final = finals.get(frozenset((normalize(entry["home_team"]),
                                          normalize(entry["away_team"]))))
            if not final:
                continue
            margin = float(final["home_margin"])
            if normalize(final["home_team"]) != normalize(entry["home_team"]):
                margin = -margin
            results[game_id] = margin

        if results:
            note = f"week {candidate}"
            return results, note if resolved else f"{note}, guessed from a short calendar"

    asked = "/".join(str(w) for w in weeks_to_try)
    return {}, f"week {asked} matched nothing" + ("" if resolved else " (guessed)")


def _settlement_brackets(game_ids, settled: list = None) -> dict[str, tuple]:
    """Bracket each game's final margin from its settled Kalshi ladder.

    The stored event id IS the Kalshi event ticker, so this needs no team-name
    matching, no week number, no calendar and no API key -- which is the whole
    point of having it.  It is the one grading path that survives every other
    input being wrong, and a calendar cached two weeks deep in September is how
    that stopped being hypothetical.
    """
    wanted = set(game_ids)
    if not wanted:
        return {}
    try:
        markets = kalshi.fetch_settled_markets() if settled is None else settled
    except Exception:                                       # noqa: BLE001
        return {}

    out = {}
    for ticker, rungs in kalshi.settled_by_event(markets).items():
        if ticker not in wanted:
            continue
        home = kalshi.home_abbrev_from_event(ticker, rungs)
        if home is None:
            continue
        bracket = kalshi.margin_from_settled(rungs, home)
        if bracket is not None:
            out[ticker] = bracket
    return out


def grade_week(week_key: str, season: int, history_dir: str = None,
               settled: list = None) -> Optional[dict]:
    """Grade a stored week from final scores, falling back to settled ladders.

    Two sources, in order of precision.  CFBD's finals give the margin itself.
    Where they reach nothing -- a wrong week number, a missing key, an FCS
    opponent CFBD does not carry -- the settled Kalshi ladder brackets it
    instead, which is enough to decide a pick whose number sits outside the
    bracket.  Checked against CFBD's own grades over 163 settled games the two
    agreed on every one.

    `settled` is the settled-market list, so a sweep over several weeks fetches
    it once; None fetches it on demand, and only for games still missing a
    result.
    """
    week = load_week(week_key, history_dir)
    if not week.get("games"):
        return None

    # Resolve the week from the week being graded, not from today. Grading a
    # stored week must not depend on when the grading happens to run.
    try:
        saturday = datetime.date.fromisoformat(week_key)
    except ValueError:
        return None

    results, note = _finals_for(week, season, saturday)
    ungraded = [gid for gid, entry in week["games"].items()
                if gid not in results and not entry.get("result")]
    brackets = _settlement_brackets(ungraded, settled)

    # A grading run that reached nothing used to return quietly, which is how
    # a whole week of picks sat unscored for a fortnight while the page showed
    # a stale column and looked fine.
    print(f"grade {week_key}: {len(results)} from finals ({note}), "
          f"{len(brackets)} of {len(ungraded)} remaining from settlements")

    week = apply_results(week, results, brackets)
    save_week(week, history_dir)
    return week


def needs_grading(week: dict, now: datetime.datetime = None,
                  max_age_days: int = 21) -> bool:
    """Whether a stored week still has results worth chasing.

    Some games never grade -- FCS matchups CFBD does not carry, mostly -- so a
    week is abandoned once it is `max_age_days` old rather than swept forever.
    """
    now = now or datetime.datetime.now(UTC)
    try:
        saturday = datetime.date.fromisoformat(week.get("week_key", ""))
    except ValueError:
        return False
    if (now.date() - saturday).days > max_age_days:
        return False

    for entry in week.get("games", {}).values():
        if entry.get("result"):
            continue
        kickoff = weeks.parse_ts(entry.get("kickoff"))
        if kickoff and kickoff < now - datetime.timedelta(hours=6):
            return True
    return False


def grade_recent(season: int, history_dir: str = None,
                 max_age_days: int = 21) -> dict[str, int]:
    """Grade every recent week that still has finished games without results.

    Grading used to run only for the current week_key, so a final that landed
    after the week rolled over was lost for good: 17 already-played games from
    one week were stranded that way. Sweeping back picks them up, and settled
    weeks stop being fetched once complete, so the steady state is unchanged.

    The settled-market list is fetched at most once for the whole sweep, and
    only once a week has actually asked for it -- so a run with nothing left to
    grade still costs nothing.
    """
    history_dir = history_dir or config.HISTORY_DIR
    graded: dict[str, int] = {}
    if not os.path.isdir(history_dir):
        return graded

    settled = None
    for name in sorted(os.listdir(history_dir), reverse=True):
        if not name.endswith(".json"):
            continue
        week = load_week(name[:-5], history_dir)
        if not needs_grading(week, max_age_days=max_age_days):
            continue
        if settled is None:
            # Grading no longer requires a CFBD key, so this is not gated on
            # one: settlements are the fallback precisely for the runs where
            # the key, the calendar or the week number is the thing at fault.
            try:
                settled = kalshi.fetch_settled_markets()
            except Exception:                               # noqa: BLE001
                settled = []
        try:
            result = grade_week(week["week_key"], season, history_dir, settled=settled)
        except Exception:                                   # noqa: BLE001
            continue
        if result:
            graded[week["week_key"]] = sum(
                1 for e in result["games"].values() if e.get("result"))
    return graded


# --------------------------------------------------------------------------
# Scoreboard
# --------------------------------------------------------------------------

def _outcome(entry: dict, lock: str, lens: str) -> Optional[bool]:
    result = entry.get("result") or {}
    if lens == "master":
        return result.get(f"{lock}_correct")
    return ((result.get("lenses") or {}).get(lens) or {}).get(f"{lock}_correct")


def lock_moment(entry: dict, lock: str) -> Optional[datetime.datetime]:
    """When this lock is due to fire for this game, or None without a kickoff."""
    kickoff = weeks.parse_ts(entry.get("kickoff"))
    if kickoff is None:
        return None
    return (weeks.decision_deadline(kickoff) if lock == "decision"
            else weeks.final_lock(kickoff))


def _has_fired(entry: dict, lock: str, now: datetime.datetime) -> bool:
    """Has this lock's moment passed -- is this row frozen, or still moving?

    Until the moment arrives, record() overwrites the snapshot on every run, so
    what is stored is the live board: the pick as it stands right now, which can
    still change or vanish before the lock takes it. Both readings of that are
    wrong on their own. Hiding it leaves a record showing nothing while the
    Master tab shows six picks, with no way to reconcile the two. Printing it
    as a pick already made claims a commitment days early.

    So the row is shown either way and this decides how: a fired lock is a
    commitment and reads as one, an unfired lock is marked live and says when it
    locks. It counts toward no tally -- an ungraded pick never could -- so the
    numbers stay honest while the list stays complete.
    """
    moment = lock_moment(entry, lock)
    return True if moment is None else now >= moment


def _in_scope(entry: dict, lock: str, tiers, versions=None) -> bool:
    """Whether this game belongs in a record at all.

    Tier is judged at the lock in question, not inherited from another one.
    A game's tier moves: with volume piling in late, Missouri/Kansas went from a
    0.30 band to 0.56 and dropped A to B between the two locks. Filtering on the
    tier recorded at the lock being graded is the only reading that matches what
    the record claims to measure.

    Whether the lock has fired is deliberately NOT asked here. Both master
    records now carry every pick the board shows from the moment it appears, and
    freeze it when their own clock runs out; see _has_fired.

    `versions` scopes a record to one or more STRATEGY_VERSIONs. A record that
    blends two formulas is two records added together: the published Final
    (T-1h) 3-2 was v1 going 1-2 plus v2 going 2-0. An unstamped snapshot
    predates versioning entirely, so it is never the current formula."""
    if versions and _version(_lock(entry, lock)) not in versions:
        return False
    if not tiers:
        return True
    return ((_lock(entry, lock) or {}).get("tier")) in tiers


def _load_weeks(history_dir: str) -> list[dict]:
    out = []
    if os.path.isdir(history_dir):
        for name in sorted(os.listdir(history_dir)):
            if name.endswith(".json"):
                with open(os.path.join(history_dir, name), encoding="utf-8") as fh:
                    out.append(json.load(fh))
    return _one_file_per_game(out)


def _one_file_per_game(weeks_data: list[dict]) -> list[dict]:
    """Drop a game from every week file but the one its kickoff belongs to.

    record() files a game under the week the RUN happens in, so a game the board
    carries early lands in the previous week's file as well -- 27 of them across
    the first three stored weeks. That was harmless while results came only from
    CFBD, which matches finals by week number and so could never reach the early
    copy. Settled ladders are matched on the Kalshi event ticker, which is the
    SAME ticker in both files, so both copies grade and both would be counted.

    Not one stale copy currently carries a pick -- a game that far from kickoff
    has no signal to pick on -- so this guards a hazard rather than correcting a
    live miscount. It is still worth guarding: a record whose entire claim is
    that it cannot be revised must not be able to count one game twice.

    The early copy is kept while it is the ONLY one. Between the Saturday a game
    first appears on the board and the week rolling over, that copy is the only
    place the record can see it, and dropping it would hide a live pick.
    """
    present: dict[str, set] = {}
    for week in weeks_data:
        for gid in (week.get("games") or {}):
            present.setdefault(gid, set()).add(week.get("week_key"))

    out = []
    for week in weeks_data:
        key = week.get("week_key")
        games = {}
        for gid, entry in (week.get("games") or {}).items():
            kickoff = weeks.parse_ts(entry.get("kickoff"))
            belongs = weeks.week_key(kickoff) if kickoff else key
            if belongs != key and belongs in present.get(gid, ()):
                continue
            games[gid] = entry
        out.append({**week, "games": games})
    return out


def _tally(entries: list[dict], lock: str, lens: str = "master", tiers=None,
           versions=None) -> dict:
    wins = losses = 0
    for e in entries:
        if not _in_scope(e, lock, tiers, versions):
            continue
        ok = _outcome(e, lock, lens)
        if ok is True:
            wins += 1
        elif ok is False:
            losses += 1
    total = wins + losses
    return {"wins": wins, "losses": losses, "total": total,
            "pct": round(wins / total, 4) if total else None}


def _open(entries: list[dict], lock: str, lens: str, tiers, versions,
          now: datetime.datetime) -> dict:
    """Picks this record is carrying that no result has landed on yet.

    Split by whether their lock has fired, because the two mean different
    things: `live` is what the board is showing right now and can still change,
    `pending` is frozen and waiting on a final score. Without this the row for a
    record with nothing graded reads a bare em dash, which is indistinguishable
    from a record making no picks at all -- the exact confusion of a board
    showing six master picks above a headline record showing none.
    """
    live = pending = 0
    for e in entries:
        if e.get("result"):
            continue
        if not _in_scope(e, lock, tiers, versions):
            continue
        if not ((_picks(_lock(e, lock)) or {}).get(lens) or {}).get("side"):
            continue
        if _has_fired(e, lock, now):
            pending += 1
        else:
            live += 1
    return {"live": live, "pending": pending}


def _ungraded_week(weeks_data: list[dict], now: datetime.datetime) -> Optional[dict]:
    """The newest stored week that has been played and carries no results.

    On the page a record whose newest column is a fortnight old looks exactly
    like one that had a quiet week, so a whole slate failing to grade shows up
    as nothing at all: 85 games and 113 picks sat unscored for a fortnight while
    the scoreboard read as though everything were fine. Saying which week is
    missing costs one field and removes that entire failure mode.

    Only the newest PLAYED week is reported. Weeks still ahead are skipped (they
    are not late, they have not happened), and once that week grades this goes
    back to None -- an older gap is a game CFBD does not carry, not an outage.
    """
    for week in reversed(weeks_data):
        games = (week.get("games") or {}).values()
        if any(e.get("result") for e in games):
            return None
        played = [e for e in games
                  if (weeks.parse_ts(e.get("kickoff")) or now)
                  < now - datetime.timedelta(hours=6)]
        if not played:
            continue
        picks = sum(
            1 for e in played
            for _key, _label, lock, lens, tiers, versions in RECORDS
            if _in_scope(e, lock, tiers, versions)
            and ((_picks(_lock(e, lock)) or {}).get(lens) or {}).get("side")
        )
        return {"week_key": week["week_key"], "games": len(played), "picks": picks}
    return None


def summarize(history_dir: str = None, now: datetime.datetime = None) -> dict:
    """Build the stacked scoreboard the page leads with."""
    history_dir = history_dir or config.HISTORY_DIR
    now = now or datetime.datetime.now(UTC)
    weeks_data = _load_weeks(history_dir)

    graded_weeks, all_entries, every_entry = [], [], []
    for week in weeks_data:
        every_entry.extend(week.get("games", {}).values())
        entries = [e for e in week.get("games", {}).values() if e.get("result")]
        if not entries:
            continue
        graded_weeks.append({
            "week_key": week["week_key"],
            "decision": _tally(entries, "decision"),
            "final": _tally(entries, "final"),
            "records": {key: _tally(entries, lock, lens, tiers, versions)
                        for key, _, lock, lens, tiers, versions in RECORDS},
        })
        all_entries.extend(entries)

    by_tier, by_edge = {}, {}
    drifts, agreements = [], []
    for e in all_entries:
        d, c = _lock(e, "decision") or {}, _lock(e, "final") or {}
        tier = d.get("tier")
        if tier:
            by_tier.setdefault(tier, []).append(e)

        pick = (_picks(d) or {}).get("master") or {}
        if pick.get("side") and pick.get("edge") is not None:
            magnitude = abs(float(pick["edge"]))
            for label, lo, hi in EDGE_BUCKETS:
                if lo <= magnitude < hi:
                    by_edge.setdefault(label, []).append(e)
                    break

        if d.get("blended_margin") is not None and c.get("blended_margin") is not None:
            drifts.append(abs(float(c["blended_margin"]) - float(d["blended_margin"])))
            agreements.append(((_picks(d) or {}).get("master") or {}).get("side")
                              == ((_picks(c) or {}).get("master") or {}).get("side"))

    drifts.sort()
    return {
        "generated_at": now.isoformat(),
        "weeks": graded_weeks,
        "last_week": graded_weeks[-1] if graded_weeks else None,
        "season": {"decision": _tally(all_entries, "decision"),
                   "final": _tally(all_entries, "final")},
        "records": [
            {"key": key, "label": label, "lock": lock, "lens": lens,
             "tiers": list(tiers) if tiers else None,
             "versions": list(versions) if versions else None,
             "season": _tally(all_entries, lock, lens, tiers, versions),
             # What the version scope costs, so a number that shrank says why
             # rather than just being smaller than last week.
             "superseded": (_tally(all_entries, lock, lens, tiers)["total"]
                            - _tally(all_entries, lock, lens, tiers, versions)["total"]
                            if versions else 0),
             "open": _open(every_entry, lock, lens, tiers, versions, now),
             "last_week": (graded_weeks[-1]["records"].get(key) if graded_weeks else None)}
            for key, label, lock, lens, tiers, versions in RECORDS
        ],
        "lenses": [
            {"key": key, "label": label,
             "season": _tally(all_entries, "decision", key),
             "last_week": (graded_weeks[-1]["records"].get(f"lens_{key}")
                           if graded_weeks and key != "master" else
                           (graded_weeks[-1]["records"].get("thursday_all") if graded_weeks else None))}
            for key, label in LENSES
        ],
        "strategy_versions": sorted({
            ((_picks(e.get("decision")) or {}).get("master") or {}).get("strategy_version")
            for e in all_entries
        } - {None}),
        "by_tier": {k: _tally(v, "decision") for k, v in sorted(by_tier.items())},
        "by_edge": {label: _tally(by_edge.get(label, []), "decision")
                    for label, _, _ in EDGE_BUCKETS},
        "drift": {
            "median": round(drifts[len(drifts) // 2], 2) if drifts else None,
            "samples": len(drifts),
            "same_pick": sum(1 for a in agreements if a),
            "same_pick_total": len(agreements),
        },
        "graded_games": len(all_entries),
        # The newest played week that never graded, or None when there is none.
        "ungraded": _ungraded_week(weeks_data, now),
    }


# --------------------------------------------------------------------------
# Pick log
# --------------------------------------------------------------------------

def _minutes_early(entry: dict, lock: str, snap: dict) -> Optional[int]:
    """Minutes between a snapshot and the lock moment it stands in for."""
    moment = lock_moment(entry, lock)
    taken = weeks.parse_ts((snap or {}).get("at"))
    if moment is None or taken is None:
        return None
    return max(0, round((moment - taken).total_seconds() / 60))


def pick_log(history_dir: str = None, now: datetime.datetime = None) -> dict:
    """Every individual pick behind every record, newest first.

    A tally on its own cannot tell you whether a losing week was bad calls or
    good calls that lost on the number, so each entry carries the line it was
    made against next to the final margin.

    Three states, not two. A pick whose lock has fired but whose game has not
    been played is `pending`; one whose lock is still ahead is `live`, still
    moving with the board and carrying the moment it will freeze. Both are
    listed, because a record that only fills in after the fact is exactly when
    it is least useful -- and because the Master tab and the record it is
    measured by have to show the same games. Neither reaches a tally: only a
    graded pick is a win or a loss.

    A fourth state exists but is rare: `unresolved`, a game that has been played
    and settled where the evidence brackets the margin without reaching this
    pick's number. Over the 208 graded picks the settlement path was checked
    against, it happened to none of them -- but it is what honest looks like
    when it does.
    """
    history_dir = history_dir or config.HISTORY_DIR
    now = now or datetime.datetime.now(UTC)
    entries = []

    for week in _load_weeks(history_dir):
        for game in week.get("games", {}).values():
            result = game.get("result") or {}
            margin = result.get("home_margin")

            for key, _label, lock, lens, tiers, versions in RECORDS:
                snap = _lock(game, lock)
                if not snap or not _in_scope(game, lock, tiers, versions):
                    continue
                pick = (_picks(snap) or {}).get(lens) or {}
                if not pick.get("side"):
                    continue

                fired = _has_fired(game, lock, now)
                moment = lock_moment(game, lock)
                if result:
                    ok = _outcome(game, lock, lens)
                    if ok is not None:
                        outcome = "win" if ok else "loss"
                    elif margin is not None:
                        outcome = "push"
                    else:
                        # Played and settled, but the bracket the settlement
                        # gives straddles this pick's number, so nothing decides
                        # it. Named rather than filed as a push, which would
                        # claim the game landed exactly on the line.
                        outcome = "unresolved"
                else:
                    outcome = "pending" if fired else "live"

                entries.append({
                    "record": key,
                    "week": week.get("week_key"),
                    "game": game.get("title"),
                    "home_team": game.get("home_team"),
                    "away_team": game.get("away_team"),
                    "kickoff": game.get("kickoff"),
                    "tier": snap.get("tier"),
                    "side": pick.get("side"),
                    "team": pick.get("team"),
                    "line": pick.get("line"),
                    "estimate": pick.get("estimate"),
                    "edge": round(float(pick["edge"]), 2) if pick.get("edge") is not None else None,
                    "p_cover": pick.get("p_cover"),
                    "confidence": pick.get("confidence"),
                    "strategy_version": _version(snap),
                    # Whether this row is frozen. A live row is the board's
                    # current pick and will be rewritten on the next run.
                    "locked": fired,
                    "locks_at": moment.isoformat() if moment else None,
                    "locked_at": snap.get("at") if fired else None,
                    "minutes_before_kickoff": (snap.get("minutes_before_kickoff")
                                               if fired else None),
                    # How far ahead of its own moment this snapshot was taken.
                    # Healthy runs land within a cron interval of the lock; a
                    # big number means the game stopped appearing in the slate
                    # and the lock froze early, so the record is not measuring
                    # what its name says. Meaningless before the lock fires --
                    # a live snapshot is SUPPOSED to predate its own moment --
                    # so it is only reported once the row is frozen.
                    "minutes_before_lock": (_minutes_early(game, lock, snap)
                                            if fired else None),
                    "dollar_volume": snap.get("dollar_volume"),
                    "home_margin": margin,
                    # Where the margin is only bracketed, the bounds, so the
                    # page can say "by 4-5" instead of leaving the column blank.
                    "margin_low": result.get("margin_low"),
                    "margin_high": result.get("margin_high"),
                    "result": outcome,
                })

    entries.sort(key=lambda e: (e.get("kickoff") or "", e.get("game") or ""), reverse=True)
    return {"generated_at": datetime.datetime.now(UTC).isoformat(), "entries": entries}


def write_picks(log: dict, data_dir: str = None) -> str:
    data_dir = data_dir or config.DATA_DIR
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "picks.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(log, fh, separators=(",", ":"))
        fh.write("\n")
    return path


def write_summary(summary: dict, data_dir: str = None) -> str:
    data_dir = data_dir or config.DATA_DIR
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "accuracy.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
        fh.write("\n")
    return path
