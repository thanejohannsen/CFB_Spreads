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


def current_week(year: int) -> Optional[int]:
    """Which regular-season week we are in, from the CFBD calendar."""
    import datetime
    try:
        cal = _get("/calendar", {"year": year})
    except (HttpError, MissingKey):
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    best = None
    for entry in cal:
        if (_field(entry, "seasonType", "season_type") or "regular") != "regular":
            continue
        last = _field(entry, "lastGameStart", "last_game_start", "endDate", "end_date")
        week = _field(entry, "week")
        if not last or week is None:
            continue
        try:
            end = datetime.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except ValueError:
            continue
        if end.tzinfo is None:
            end = end.replace(tzinfo=datetime.timezone.utc)
        if now <= end + datetime.timedelta(days=1):
            if best is None or week < best:
                best = week
    return best


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
    stamp = (cache.get(key) or {}).get("fetched_at")
    if not stamp:
        return False
    try:
        when = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return (_now() - when).total_seconds() < max_age_hours * 3600


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

    cache[key] = {"fetched_at": _now().isoformat(), "games": games, "lines": lines, "sp": sp}
    # Keep the file small: only the most recent few weeks are ever needed.
    for stale in sorted(cache.keys())[:-4]:
        cache.pop(stale, None)
    save_cache(cache, path)
    return games, lines, sp, True
