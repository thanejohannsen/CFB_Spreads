"""Vegas lines and final scores from the CollegeFootballData API.

Requires a free API key (collegefootballdata.com/key) supplied as the
CFBD_API_KEY environment variable -- in GitHub Actions that comes from a
repository secret and is never shipped to the browser.

Without a key the pipeline still runs: Kalshi's implied spreads and the
uncertainty bands are unaffected, and the manual spread box on the page covers
the games you actually care about.  Only the automatic Vegas baseline and
score-based grading go dark.
"""

from __future__ import annotations

import os
from typing import Optional

from .http import get_json, HttpError

CFBD_BASE = "https://api.collegefootballdata.com"


class MissingKey(RuntimeError):
    pass


def api_key() -> Optional[str]:
    key = os.environ.get("CFBD_API_KEY", "").strip()
    return key or None


def available() -> bool:
    return api_key() is not None


def _get(path: str, params: dict) -> list:
    key = api_key()
    if not key:
        raise MissingKey("CFBD_API_KEY is not set")
    data = get_json(f"{CFBD_BASE}{path}", params, headers={"Authorization": f"Bearer {key}"})
    return data if isinstance(data, list) else []


def _field(obj: dict, *names, default=None):
    """CFBD has shipped both snake_case and camelCase field names; accept either."""
    for n in names:
        if n in obj and obj[n] is not None:
            return obj[n]
    return default


def fetch_lines(year: int, week: int, season_type: str = "regular") -> list[dict]:
    """Betting lines for a week, normalised to a home-favoured-by convention.

    CFBD quotes `spread` from the home team's perspective and negative when the
    home team is favoured, so a home favourite of 7 arrives as -7.  Everything
    downstream uses `home_favored_by`, positive when the home team gives points.
    """
    raw = _get("/lines", {"year": year, "week": week, "seasonType": season_type})
    out = []
    for g in raw:
        providers = _field(g, "lines", default=[]) or []
        picks = []
        for p in providers:
            spread = _field(p, "spread")
            if spread is None:
                continue
            try:
                picks.append({
                    "provider": _field(p, "provider", default="?"),
                    "home_favored_by": -float(spread),
                    "over_under": _field(p, "overUnder", "over_under"),
                })
            except (TypeError, ValueError):
                continue
        if not picks:
            continue
        consensus = sorted(p["home_favored_by"] for p in picks)[len(picks) // 2]
        out.append({
            "cfbd_id": _field(g, "id"),
            "season": _field(g, "season"),
            "week": _field(g, "week"),
            "home_team": _field(g, "homeTeam", "home_team"),
            "away_team": _field(g, "awayTeam", "away_team"),
            "start_date": _field(g, "startDate", "start_date"),
            "home_favored_by": consensus,
            "books": picks,
        })
    return out


def fetch_games(year: int, week: int, season_type: str = "regular") -> list[dict]:
    """Schedule and final scores for a week."""
    raw = _get("/games", {"year": year, "week": week, "seasonType": season_type})
    out = []
    for g in raw:
        hp = _field(g, "homePoints", "home_points")
        ap = _field(g, "awayPoints", "away_points")
        out.append({
            "cfbd_id": _field(g, "id"),
            "home_team": _field(g, "homeTeam", "home_team"),
            "away_team": _field(g, "awayTeam", "away_team"),
            "start_date": _field(g, "startDate", "start_date"),
            "completed": bool(_field(g, "completed", default=False)),
            "neutral_site": bool(_field(g, "neutralSite", "neutral_site", default=False)),
            "home_points": hp,
            "away_points": ap,
            "home_margin": (float(hp) - float(ap)) if hp is not None and ap is not None else None,
        })
    return out


def fetch_sp_ratings(year: int) -> dict[str, float]:
    """SP+ overall ratings, used only as a fallback prior when no line exists."""
    try:
        raw = _get("/ratings/sp", {"year": year})
    except (HttpError, MissingKey):
        return {}
    out = {}
    for r in raw:
        team, rating = _field(r, "team"), _field(r, "rating")
        if team is not None and rating is not None:
            try:
                out[team] = float(rating)
            except (TypeError, ValueError):
                pass
    return out


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------
#
# The Kalshi snapshot runs often -- every 20-30 minutes through the decision
# window -- but CFBD allows only 1,000 calls a month on the free tier.  Betting
# lines move far more slowly than that cadence, so responses are cached in a
# small committed file and refreshed a few times a day.  That keeps the monthly
# call count near 180 while leaving the Kalshi cadence untouched.

import datetime
import json
import math
import re

CACHE_PATH = "docs/data/cfbd_snapshot.json"
MAX_AGE_HOURS = 8

# Bump whenever a cached entry gains or changes a field. An entry written by an
# older version is stale no matter how recent it is: without this, adding SP+
# ratings meant every run inside the 8-hour window kept serving an entry that
# predated them, and the new field silently stayed empty.
CACHE_SCHEMA = 2


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def load_cache(path: str = CACHE_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict, path: str = CACHE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")


_WEEKLY_KEY = re.compile(r"^\d{4}-\d+$")
_FINALS_KEY = re.compile(r"^finals-\d{4}-\d+$")


def _prune(cache: dict, weeks_kept: int = 4, finals_kept: int = 6) -> None:
    """Keep the snapshot small, dropping the OLDEST of each kind.

    This was `for stale in sorted(cache.keys())[:-4]`, which prunes
    alphabetically across every key in the file -- not the same set and not the
    same order. With four `finals-` entries present it evicts `calendar-2026`
    AND the week entry the caller has just written, in the same call, so the
    next run refetches both; on Sep 20 it had already dropped both live week
    caches while keeping two stale finals. A cache written to spend fewer API
    calls was arranging to spend more.

    So: only weekly game caches and finals are ever dropped, oldest first, and
    the calendar -- one call a season, and the thing everything else is keyed
    off -- is never dropped at all. Finals outlive the 21-day grading sweep by
    enough weeks that nothing is refetched to grade.
    """
    for pattern, keep in ((_WEEKLY_KEY, weeks_kept), (_FINALS_KEY, finals_kept)):
        matching = [k for k in cache if pattern.match(k)]
        matching.sort(key=lambda k: (cache.get(k) or {}).get("fetched_at") or "")
        for stale in matching[:-keep] if keep else matching:
            cache.pop(stale, None)


def _fresh(cache: dict, key: str, max_age_hours: float) -> bool:
    entry = cache.get(key) or {}
    if entry.get("schema") != CACHE_SCHEMA:
        return False                        # written by an older shape
    stamp = entry.get("fetched_at")
    if not stamp:
        return False
    try:
        when = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return (_now() - when).total_seconds() < max_age_hours * 3600


def _parse_dt(value) -> Optional["datetime.datetime"]:
    import datetime as _dt
    if not value:
        return None
    try:
        dt = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=_dt.timezone.utc) if dt.tzinfo is None else dt


def _covers(weeks: list[dict], target) -> bool:
    """Does this calendar have a week containing `target`?"""
    return any(w["start"].date() <= target <= w["end"].date() for w in weeks)


def _weeks_of(entry: dict) -> list[dict]:
    return [{"week": w["week"], "start": _parse_dt(w["start"]), "end": _parse_dt(w["end"])}
            for w in (entry.get("weeks") or [])]


def calendar_weeks(year: int, path: str = CACHE_PATH, covering=None) -> list[dict]:
    """Regular-season weeks with their date ranges.

    Cached hard: a season's calendar does not change, and this used to be
    fetched on every single run -- roughly 780 calls a month at the current
    cadence, which on its own would have pushed the free tier over its 1,000
    limit.

    But a season's calendar not changing is not the same as the API returning
    all of it. The 2026 entry came back on Sep 14 holding two weeks -- 2 and 3,
    Sep 8 to Sep 21 -- and was then cached for a fortnight without being asked
    again, so every later date fell outside every range.

    So the cache is trusted only while it answers the question being asked:
    pass `covering` and a calendar that does not reach that date is refetched.
    Retried no more often than any other entry, though, because a calendar that
    genuinely stops short would otherwise refetch on EVERY call -- five a run,
    which is the 1,000-call tier gone in a week. It is asked again, not asked
    repeatedly, and `week_for_date_checked` no longer depends on the answer.
    """
    cache = load_cache(path)
    key = f"calendar-{year}"
    entry = cache.get(key) or {}
    cached = _weeks_of(entry) if entry.get("schema") == CACHE_SCHEMA else []
    if cached and (covering is None or _covers(cached, covering)
                   or _fresh(cache, key, MAX_AGE_HOURS)):
        return cached

    try:
        raw = _get("/calendar", {"year": year})
    except (HttpError, MissingKey):
        # A failed refresh must not throw away what we already had; a partial
        # calendar still resolves the weeks it does cover.
        return cached

    weeks = []
    for e in raw:
        if (_field(e, "seasonType", "season_type") or "regular") != "regular":
            continue
        week = _field(e, "week")
        start = _parse_dt(_field(e, "firstGameStart", "first_game_start", "startDate", "start_date"))
        end = _parse_dt(_field(e, "lastGameStart", "last_game_start", "endDate", "end_date"))
        if week is None or start is None or end is None:
            continue
        weeks.append({"week": int(week), "start": start, "end": end})
    weeks.sort(key=lambda w: w["week"])

    # Say what came back. A calendar two weeks long is not visibly different
    # from a complete one anywhere else in the output, and that is precisely
    # why it went unnoticed for a fortnight.
    print(f"cfbd: calendar {year}: {len(weeks)} of {len(raw)} entries usable"
          + (f", weeks {weeks[0]['week']}-{weeks[-1]['week']}, "
             f"{weeks[0]['start'].date()} to {weeks[-1]['end'].date()}" if weeks else ""))

    # Record the attempt either way, so an endpoint that keeps returning a
    # short calendar is asked again on the next cache cycle rather than on the
    # next call.
    cache[key] = {"schema": CACHE_SCHEMA, "fetched_at": _now().isoformat(),
                  "weeks": ([{"week": w["week"], "start": w["start"].isoformat(),
                              "end": w["end"].isoformat()} for w in weeks]
                            or entry.get("weeks") or [])}
    save_cache(cache, path)
    return weeks or cached


def week_for_date(year: int, target, path: str = CACHE_PATH) -> Optional[int]:
    """The CFBD week containing `target` (a date).

    This deliberately asks "which week are these games in?" rather than "which
    week is it now?".  Kalshi lists the upcoming slate days ahead while a CFBD
    week runs through its own last game -- including the occasional Sunday or
    Monday game -- so on a Monday the tool is modelling next Saturday while CFBD
    still calls it last week.  Keying off the games themselves removes the
    ambiguity: the week always follows what is actually on the board.
    """
    week, _resolved = week_for_date_checked(year, target, path)
    return week


def _modal_date(games: list) -> Optional["datetime.date"]:
    """The date most of a week's games are played on.

    The same question `build_predictions._slate_date` asks of the Kalshi slate,
    asked of CFBD's answer -- so the two can be compared directly.
    """
    import collections
    dates = [str(g.get("start_date") or "")[:10] for g in games]
    dates = [d for d in dates if d]
    if not dates:
        return None
    try:
        return datetime.date.fromisoformat(collections.Counter(dates).most_common(1)[0][0])
    except ValueError:
        return None


def _weeks_apart(played, target) -> int:
    """How many CFBD weeks separate a week's own Saturday from `target`.

    A CFBD week runs Tuesday to Monday around its Saturday, so the window is
    four days before to two days after; shifting by four turns that into a plain
    floor division, and 0 means the target belongs to this week.
    """
    return math.floor(((target - played).days + 4) / 7)


def _verify_week(year: int, candidate: int, target, path: str, hops: int = 3) -> tuple:
    """Confirm a week number against the dates of its own games.

    The calendar is metadata and has already been wrong; the games are the
    thing being asked about. Each hop costs at most one cached CFBD week -- and
    on the path that matters it costs nothing, because it is the same week the
    board is about to ask for anyway.
    """
    for _ in range(hops):
        if candidate < 1:
            return None, False
        games, _lines, _sp, _refreshed = cached_week(year, candidate, path)
        played = _modal_date(games)
        if played is None:
            return candidate, False         # nothing came back to check against
        step = _weeks_apart(played, target)
        if step == 0:
            return candidate, True
        candidate += step
    return candidate, False


def week_for_date_checked(year: int, target, path: str = CACHE_PATH) -> tuple:
    """As week_for_date, plus whether the answer was confirmed or guessed.

    Three ways to answer, in order of how much they can be trusted:

      1. The calendar covers the date.  The ordinary path, and free.
      2. It does not, so the week is extrapolated past its last week -- they are
         seven days apart -- and then CHECKED against the games of the week it
         lands on, stepping again if they say a different week. This is what
         survives a calendar that stops short, which is not hypothetical: the
         cached 2026 calendar ended at week 3 on Sep 21 and every date after it
         resolved to week 3 with total confidence, costing one slate its Vegas
         lines and another its entire grading run.
      3. Nothing could be checked -- no key, no games, CFBD unreachable. Fall
         back to the nearest week, which still beats no board, and say it is a
         guess so a caller can treat it as one.
    """
    weeks = calendar_weeks(year, path, covering=target)
    if not weeks or target is None:
        return None, False

    for w in weeks:
        if w["start"].date() <= target <= w["end"].date():
            return w["week"], True

    nearest = min(weeks, key=lambda w: abs((w["start"].date() - target).days))
    last = weeks[-1]
    if target > last["end"].date():
        # Past the end of the calendar -- the observed failure. `end` is a real
        # game start, so whole weeks from it lands on the right one directly.
        candidate = last["week"] + math.ceil((target - last["end"].date()).days / 7)
        week, resolved = _verify_week(year, candidate, target, path)
        if resolved:
            return week, True

    # Before the calendar begins, or the games could not confirm anything:
    # the nearest week, flagged as the guess it is.
    return nearest["week"], False


def _shared_games(cache: dict, season: int, week: int,
                  max_age_hours: float) -> Optional[list]:
    """The board's copy of this week's games, when it is good enough to grade on.

    `cached_week` and `cached_finals` store the output of the same
    `fetch_games` call under two different keys, so the same week was being
    fetched twice and the two halves of a run could disagree about it. They did:
    the run on Sep 28 cached 283 completed week-4 games for the board at
    13:04:35 while grading, seconds later, asked CFBD for week 3 and matched
    nothing -- the finals it needed were already in this file.

    Only a fresh copy will do, or one where every game has finished; a stale one
    could be missing the very scores being looked for.
    """
    shared = cache.get(f"{season}-{week}") or {}
    games = shared.get("games")
    if shared.get("schema") != CACHE_SCHEMA or not games:
        return None
    if _fresh(cache, f"{season}-{week}", max_age_hours):
        return games
    return games if all(g.get("completed") for g in games) else None


def cached_finals(season: int, week: int, path: str = CACHE_PATH,
                  max_age_hours: float = MAX_AGE_HOURS) -> list:
    """Final scores for a week, for grading.

    Only /games, not lines or ratings -- grading needs none of those. Results
    are immutable once every game has finished, so a settled week is fetched
    once and never again; that is what keeps sweeping back over old weeks from
    costing anything in the steady state.
    """
    cache = load_cache(path)
    key = f"finals-{season}-{week}"
    entry = cache.get(key) or {}

    if entry.get("schema") == CACHE_SCHEMA:
        if entry.get("complete") or _fresh(cache, key, max_age_hours):
            return entry.get("games", [])

    games = _shared_games(cache, season, week, max_age_hours)
    if games is None:
        try:
            games = fetch_games(season, week)
        except Exception:                                   # noqa: BLE001
            return entry.get("games", [])

    cache[key] = {
        "schema": CACHE_SCHEMA, "fetched_at": _now().isoformat(), "games": games,
        "complete": bool(games) and all(g.get("completed") for g in games),
    }
    save_cache(cache, path)
    return games


def cached_week(season: int, week: int, path: str = CACHE_PATH,
                max_age_hours: float = MAX_AGE_HOURS) -> tuple[list, list, dict, bool]:
    """Games, lines and SP+ ratings for a week, refetched when the cache ages out.

    Returns (games, lines, sp_ratings, refreshed).  On a fetch failure the
    previous cached values are returned rather than nothing, so one bad call
    cannot blank the Vegas column on the page.
    """
    cache = load_cache(path)
    key = f"{season}-{week}"
    entry = cache.get(key) or {}

    if _fresh(cache, key, max_age_hours):
        return entry.get("games", []), entry.get("lines", []), entry.get("sp", {}), False

    try:
        games, lines = fetch_games(season, week), fetch_lines(season, week)
    except Exception:                                       # noqa: BLE001
        return entry.get("games", []), entry.get("lines", []), entry.get("sp", {}), False

    # SP+ is a lens, not the main event: if it fails, keep whatever was cached
    # and let the page say so rather than failing the whole run.
    sp = fetch_sp_ratings(season) or entry.get("sp", {})

    cache[key] = {"schema": CACHE_SCHEMA, "fetched_at": _now().isoformat(),
                  "games": games, "lines": lines, "sp": sp}
    _prune(cache)
    save_cache(cache, path)
    return games, lines, sp, True
