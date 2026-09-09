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


def _snapshot(game: dict, at: datetime.datetime) -> dict:
    """The fields worth freezing; the full survival table is not one of them."""
    return {
        "at": at.isoformat(),
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
        "pick": game.get("pick"),
    }


def record(payload: dict, history_dir: str = None, now: datetime.datetime = None) -> str:
    """Fold the current slate into this week's history file.

    A lock is refreshed only while its moment is still ahead; once passed it is
    frozen for good.  That yields exactly "the last snapshot before the
    deadline" without needing to guess which run will be the last.
    """
    now = now or datetime.datetime.now(UTC)
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

        if now < weeks.decision_deadline(kickoff):
            entry["decision"] = _snapshot(game, now)
        if now < kickoff:
            entry["closing"] = _snapshot(game, now)

    return save_week(week, history_dir)


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------

def _graded(snapshot: Optional[dict], home_margin: float) -> Optional[bool]:
    """Did this snapshot's pick cover?  None when it made no pick or pushed."""
    if not snapshot:
        return None
    pick = snapshot.get("pick") or {}
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
            "decision_correct": _graded(entry.get("decision"), margin),
            "closing_correct": _graded(entry.get("closing"), margin),
        }
    return week


def grade_week(week_key: str, season: int, history_dir: str = None) -> Optional[dict]:
    """Pull finals from CFBD and grade a stored week."""
    if not cfbd.available():
        return None
    week = load_week(week_key, history_dir)
    if not week.get("games"):
        return None

    cfb_week = cfbd.current_week(season)
    if cfb_week is None:
        return None

    from .match_games import normalize
    finals = {}
    for g in cfbd.fetch_games(season, cfb_week):
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


# --------------------------------------------------------------------------
# Scoreboard
# --------------------------------------------------------------------------

def _tally(entries: list[dict], lock: str) -> dict:
    wins = losses = 0
    for e in entries:
        ok = (e.get("result") or {}).get(f"{lock}_correct")
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
    weeks_data = []
    if os.path.isdir(history_dir):
        for name in sorted(os.listdir(history_dir)):
            if name.endswith(".json"):
                with open(os.path.join(history_dir, name), encoding="utf-8") as fh:
                    weeks_data.append(json.load(fh))

    graded_weeks, all_entries = [], []
    for week in weeks_data:
        entries = [e for e in week.get("games", {}).values() if e.get("result")]
        if not entries:
            continue
        graded_weeks.append({"week_key": week["week_key"],
                             "decision": _tally(entries, "decision"),
                             "closing": _tally(entries, "closing")})
        all_entries.extend(entries)

    by_tier, by_edge = {}, {}
    drifts, agreements = [], []
    for e in all_entries:
        d, c = e.get("decision") or {}, e.get("closing") or {}
        tier = d.get("tier")
        if tier:
            by_tier.setdefault(tier, []).append(e)

        pick = d.get("pick") or {}
        if pick.get("side") and pick.get("edge") is not None:
            magnitude = abs(float(pick["edge"]))
            for label, lo, hi in EDGE_BUCKETS:
                if lo <= magnitude < hi:
                    by_edge.setdefault(label, []).append(e)
                    break

        if d.get("blended_margin") is not None and c.get("blended_margin") is not None:
            drifts.append(abs(float(c["blended_margin"]) - float(d["blended_margin"])))
            agreements.append((d.get("pick") or {}).get("side") == (c.get("pick") or {}).get("side"))

    drifts.sort()
    return {
        "generated_at": datetime.datetime.now(UTC).isoformat(),
        "weeks": graded_weeks,
        "last_week": graded_weeks[-1] if graded_weeks else None,
        "season": {"decision": _tally(all_entries, "decision"),
                   "closing": _tally(all_entries, "closing")},
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


def write_summary(summary: dict, data_dir: str = None) -> str:
    data_dir = data_dir or config.DATA_DIR
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "accuracy.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
        fh.write("\n")
    return path
