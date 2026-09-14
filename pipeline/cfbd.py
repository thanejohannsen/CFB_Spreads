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


def calendar_weeks(year: int, path: str = CACHE_PATH) -> list[dict]:
    """Regular-season weeks with their date ranges.

    Cached hard: a season's calendar does not change, and this used to be
    fetched on every single run -- roughly 780 calls a month at the current
    cadence, which on its own would have pushed the free tier over its 1,000
    limit.
    """
    cache = load_cache(path)
    key = f"calendar-{year}"
    entry = cache.get(key) or {}
    if entry.get("schema") == CACHE_SCHEMA and entry.get("weeks"):
        return [{"week": w["week"], "start": _parse_dt(w["start"]), "end": _parse_dt(w["end"])}
                for w in entry["weeks"]]

    try:
        raw = _get("/calendar", {"year": year})
    except (HttpError, MissingKey):
        return []

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

    if weeks:
        cache[key] = {"schema": CACHE_SCHEMA, "fetched_at": _now().isoformat(),
                      "weeks": [{"week": w["week"], "start": w["start"].isoformat(),
                                 "end": w["end"].isoformat()} for w in weeks]}
        save_cache(cache, path)
    return weeks


def week_for_date(year: int, target) -> Optional[int]:
    """The CFBD week containing `target` (a date).

    This deliberately asks "which week are these games in?" rather than "which
    week is it now?".  Kalshi lists the upcoming slate days ahead while a CFBD
    week runs through its own last game -- including the occasional Sunday or
    Monday game -- so on a Monday the tool is modelling next Saturday while CFBD
    still calls it last week.  Keying off the games themselves removes the
    ambiguity: the week always follows what is actually on the board.
    """
    weeks = calendar_weeks(year)
    if not weeks or target is None:
        return None

    for w in weeks:
        if w["start"].date() <= target <= w["end"].date():
            return w["week"]

    # Between two weeks, or past the last one: take the nearest by start date so
    # a gap in the calendar degrades to the closest week rather than to nothing.
    return min(weeks, key=lambda w: abs((w["start"].date() - target).days))["week"]


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

    try:
        games = fetch_games(season, week)
    except Exception:                                       # noqa: BLE001
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
    # Keep the file small: only the most recent few weeks are ever needed.
    for stale in sorted(cache.keys())[:-4]:
        cache.pop(stale, None)
    save_cache(cache, path)
    return games, lines, sp, True
