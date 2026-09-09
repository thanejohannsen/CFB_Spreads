"""Week keys and the decision deadline.

Picks are due Wednesday night / Thursday morning, so the headline accuracy
record locks at Thursday noon ET -- that is the number describing the tool as
actually used, rather than a flattering one taken at the kickoff liquidity peak.
"""

from __future__ import annotations

import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from . import config

ET = ZoneInfo("America/New_York")
UTC = datetime.timezone.utc


def parse_ts(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def week_saturday(when: datetime.datetime) -> datetime.date:
    """The Saturday on or after `when` -- the label for that game week."""
    d = when.astimezone(ET).date()
    return d + datetime.timedelta(days=(5 - d.weekday()) % 7)


def week_key(when: datetime.datetime) -> str:
    return week_saturday(when).isoformat()


def decision_deadline(kickoff: datetime.datetime) -> datetime.datetime:
    """Thursday noon ET of that game's week, or kickoff if it comes first."""
    saturday = week_saturday(kickoff)
    thursday = saturday - datetime.timedelta(days=(5 - config.DECISION_WEEKDAY))
    noon = datetime.datetime.combine(
        thursday, datetime.time(config.DECISION_HOUR_ET, 0), tzinfo=ET
    ).astimezone(UTC)
    return min(noon, kickoff)
