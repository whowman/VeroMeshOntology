"""What "today" means for this install.

Timestamps are stored in UTC. Dates that people reason about (an authority valid
until 31 December, "10 approvals per day", a measures period) are calendar days in
the customer's time zone, set with ``VEROMESH_TIME_ZONE`` (an IANA name such as
``America/New_York``; default ``UTC``). An approval at 20:00 in New York on
17 November counts on 17 November, not on the 18th as it would in UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_zone: tzinfo = UTC
_name = "UTC"


def configure(name: str | None) -> None:
    """Set the install's time zone; raises ValueError for an unknown name."""
    global _zone, _name
    name = (name or "UTC").strip() or "UTC"
    try:
        zone = UTC if name.upper() == "UTC" else ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(
            f"VEROMESH_TIME_ZONE {name!r} is not a time zone; use an IANA name such as America/New_York."
        ) from e
    _zone, _name = zone, name


def zone_name() -> str:
    return _name


def today() -> date:
    """Today's date in the install's time zone."""
    return datetime.now(_zone).date()


def local_date(timestamp: str) -> str:
    """The calendar day (YYYY-MM-DD, install's time zone) of a stored timestamp."""
    d = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(_zone).date().isoformat()


def end_of_day(day: date) -> datetime:
    """The last second of ``day`` in the install's time zone, as a UTC datetime."""
    start = datetime(day.year, day.month, day.day, tzinfo=_zone)
    return (start + timedelta(days=1) - timedelta(seconds=1)).astimezone(UTC)


def start_of_day(day: date) -> datetime:
    """The first instant of ``day`` in the install's time zone, as a UTC datetime."""
    return datetime(day.year, day.month, day.day, tzinfo=_zone).astimezone(UTC)
