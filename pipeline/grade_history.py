"""Lock predictions before they can be revised, then grade them.

Two locks are recorded per game:

  decision -- the last snapshot before Thursday noon ET.  This is the headline
              record, because it is the tool as actually used: picks are due
              Wednesday night / Thursday morning.
  closing  -- the last snapshot before kickoff.  Used only to measure drift, so
              the question "does a Wednesday read hold up?" gets an answer from
              evidence rather than assumption.

Neither is ever rewritten once its moment has passed.  An accuracy number you
can revise after the fact is worth nothing.
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Optional

from . import cfbd, config, weeks

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

# Every record the page can show, as (key, label, lock, lens, tier filter).
#
# The headline is the master at the T-1h lock, restricted to the tiers worth
# acting on; the Thursday row behind it keeps every tier.
#
# There was briefly an S/A-filtered Thursday row as a control, to separate the
# effect of the clock from the effect of the tier filter. It was dropped because
# it could never hold data: across the stored weeks the master picked 0 of 36
# A-tier games, since a tight band is exactly when the line sits inside it and
# the no-play rule reads no edge. A control with nothing in it is not a control.
RECORDS = [
    ("headline", "Final (T-1h) - S/A markets", "final", "master", config.HEADLINE_TIERS),
    ("thursday_all", "Thursday noon", "decision", "master", None),
] + [
    (f"lens_{key}", label, "decision", key, None)
    for key, label in LENSES if key != "master"
]


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
    """Games this week is still committed to, for build_predictions to pin.

    A lock is a commitment, and two kinds of commitment outlive a game's place
    in the top N by open interest:

      * its T-1h lock has not fired yet, so the snapshot still needs refreshing
        -- and a game missing from the payload is a game record() never sees,
        which would freeze that lock at whatever it held when the game slipped
        out of the slate;
      * it carries an actual pick, which the record grades and the board must
        therefore keep showing.

    A game that is past its last lock and was never picked is finished with:
    the record has all it needs, so it is left to fall off the board rather
    than growing the slate for the rest of the week.
    """
    now = now or datetime.datetime.now(UTC)
    out = set()
    for gid, entry in (load_week(week_key, history_dir).get("games") or {}).items():
        if not (entry.get("decision") or entry.get("final") or entry.get("closing")):
            continue
        kickoff = weeks.parse_ts(entry.get("kickoff"))
        if kickoff is not None and now < weeks.final_lock(kickoff):
            out.add(gid)
            continue
        if any(pick and pick.get("side")
               for name in ("decision", "final", "closing")
               for pick in _picks(entry.get(name)).values()):
            out.add(gid)
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

def _graded(snapshot: Optional[dict], home_margin: float,
            lens: str = "master") -> Optional[bool]:
    """Did this lens's pick cover?  None when it made no pick or pushed."""
    pick = (_picks(snapshot) or {}).get(lens) or {}
    side, line = pick.get("side"), pick.get("line")
    if side is None or line is None:
        return None
    if abs(home_margin - line) < 1e-9:
        return None                                   # push
    home_covered = home_margin > line
    return home_covered if side == "home" else not home_covered


def apply_results(week: dict, results: dict[str, float]) -> dict:
    """Attach final margins (keyed by event id) and grade both locks."""
    for game_id, entry in week.get("games", {}).items():
        margin = results.get(game_id)
        if margin is None:
            continue
        entry["result"] = {
            "home_margin": margin,
            "decision_correct": _graded(_lock(entry, "decision"), margin),
            "final_correct": _graded(_lock(entry, "final"), margin),
            "lenses": {
                key: {
                    "decision_correct": _graded(_lock(entry, "decision"), margin, key),
                    "final_correct": _graded(_lock(entry, "final"), margin, key),
                }
                for key, _ in LENSES
            },
        }
    return week


def grade_week(week_key: str, season: int, history_dir: str = None) -> Optional[dict]:
    """Pull finals from CFBD and grade a stored week."""
    if not cfbd.available():
        return None
    week = load_week(week_key, history_dir)
    if not week.get("games"):
        return None

    # Resolve the week from the week being graded, not from today. Grading a
    # stored week must not depend on when the grading happens to run.
    try:
        saturday = datetime.date.fromisoformat(week_key)
    except ValueError:
        return None
    cfb_week = cfbd.week_for_date(season, saturday)
    if cfb_week is None:
        return None

    from .match_games import normalize
    finals = {}
    for g in cfbd.cached_finals(season, cfb_week):
        if g.get("home_margin") is None or not g.get("completed"):
            continue
        finals[frozenset((normalize(g["home_team"]), normalize(g["away_team"])))] = g

    results = {}
    for game_id, entry in week["games"].items():
        key = frozenset((normalize(entry["home_team"]), normalize(entry["away_team"])))
        final = finals.get(key)
        if not final:
            continue
        margin = float(final["home_margin"])
        if normalize(final["home_team"]) != normalize(entry["home_team"]):
            margin = -margin
        results[game_id] = margin

    week = apply_results(week, results)
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
    """
    history_dir = history_dir or config.HISTORY_DIR
    graded: dict[str, int] = {}
    if not cfbd.available() or not os.path.isdir(history_dir):
        return graded

    for name in sorted(os.listdir(history_dir), reverse=True):
        if not name.endswith(".json"):
            continue
        week = load_week(name[:-5], history_dir)
        if not needs_grading(week, max_age_days=max_age_days):
            continue
        try:
            result = grade_week(week["week_key"], season, history_dir)
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
    """Has this lock's moment passed?

    Until it has, record() keeps overwriting the snapshot on every run, so what
    is stored is a live preview of what WOULD lock, not a commitment. Showing
    that in a record dated by its lock -- "Thursday noon", "Final (T-1h)" --
    reads as a pick the tool has already made, days before it has made it, and
    it can still change or disappear before the moment arrives. A record holds
    fired locks only; the board is where the live view belongs.
    """
    moment = lock_moment(entry, lock)
    return True if moment is None else now >= moment


def _in_scope(entry: dict, lock: str, tiers,
              now: datetime.datetime = None) -> bool:
    """Whether this game belongs in a record: lock fired, and tier in scope.

    Tier is judged at the lock in question, not inherited from another one.
    A game's tier moves: with volume piling in late, Missouri/Kansas went from a
    0.30 band to 0.56 and dropped A to B between the two locks. Filtering on the
    tier recorded at the lock being graded is the only reading that matches what
    the record claims to measure."""
    if not _has_fired(entry, lock, now or datetime.datetime.now(UTC)):
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
    return out


def _tally(entries: list[dict], lock: str, lens: str = "master", tiers=None) -> dict:
    wins = losses = 0
    for e in entries:
        if not _in_scope(e, lock, tiers):
            continue
        ok = _outcome(e, lock, lens)
        if ok is True:
            wins += 1
        elif ok is False:
            losses += 1
    total = wins + losses
    return {"wins": wins, "losses": losses, "total": total,
            "pct": round(wins / total, 4) if total else None}


def summarize(history_dir: str = None) -> dict:
    """Build the stacked scoreboard the page leads with."""
    history_dir = history_dir or config.HISTORY_DIR
    weeks_data = _load_weeks(history_dir)

    graded_weeks, all_entries = [], []
    for week in weeks_data:
        entries = [e for e in week.get("games", {}).values() if e.get("result")]
        if not entries:
            continue
        graded_weeks.append({
            "week_key": week["week_key"],
            "decision": _tally(entries, "decision"),
            "final": _tally(entries, "final"),
            "records": {key: _tally(entries, lock, lens, tiers)
                        for key, _, lock, lens, tiers in RECORDS},
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
        "generated_at": datetime.datetime.now(UTC).isoformat(),
        "weeks": graded_weeks,
        "last_week": graded_weeks[-1] if graded_weeks else None,
        "season": {"decision": _tally(all_entries, "decision"),
                   "final": _tally(all_entries, "final")},
        "records": [
            {"key": key, "label": label, "lock": lock, "lens": lens,
             "tiers": list(tiers) if tiers else None,
             "season": _tally(all_entries, lock, lens, tiers),
             "last_week": (graded_weeks[-1]["records"].get(key) if graded_weeks else None)}
            for key, label, lock, lens, tiers in RECORDS
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


def pick_log(history_dir: str = None) -> dict:
    """Every individual pick behind every record, newest first.

    A tally on its own cannot tell you whether a losing week was bad calls or
    good calls that lost on the number, so each entry carries the line it was
    made against next to the final margin. Picks whose game has not been played
    are included and marked pending -- otherwise the current week only appears
    in hindsight, which is when the log is least useful. Picks whose LOCK has
    not fired are a different thing and are excluded: see _has_fired.
    """
    history_dir = history_dir or config.HISTORY_DIR
    entries = []

    for week in _load_weeks(history_dir):
        for game in week.get("games", {}).values():
            result = game.get("result") or {}
            margin = result.get("home_margin")

            for key, _label, lock, lens, tiers in RECORDS:
                snap = _lock(game, lock)
                if not snap or not _in_scope(game, lock, tiers):
                    continue
                pick = (_picks(snap) or {}).get(lens) or {}
                if not pick.get("side"):
                    continue

                if margin is None:
                    outcome = "pending"
                else:
                    ok = _outcome(game, lock, lens)
                    outcome = "win" if ok is True else ("loss" if ok is False else "push")

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
                    "locked_at": snap.get("at"),
                    "minutes_before_kickoff": snap.get("minutes_before_kickoff"),
                    # How far ahead of its own moment this snapshot was taken.
                    # Healthy runs land within a cron interval of the lock; a
                    # big number means the game stopped appearing in the slate
                    # and the lock froze early, so the record is not measuring
                    # what its name says.
                    "minutes_before_lock": _minutes_early(game, lock, snap),
                    "dollar_volume": snap.get("dollar_volume"),
                    "home_margin": margin,
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
