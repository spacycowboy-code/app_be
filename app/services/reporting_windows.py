"""One definition of "today" and "this week" for every report.

Extracted from ``routers/login_stats.py`` when a second consumer (the weekly
usage report) appeared. Two copies of this arithmetic would be worse than
duplicated code: they would drift into two different week boundaries, and the
symptom is not an error but two panels that disagree by a few hours and cannot
both be right. Nothing here is new logic.

The subtle part is the time zone. ``created_at`` columns are written by the
database as *naive* values in the **DB server's** zone (UTC+2 in production),
while a day or week boundary only means anything in the **viewer's** zone
(UTC+8). So boundaries are computed in ``stats_timezone`` and then converted to
DB-local naive values before being compared with a column. Getting this wrong
shifts every boundary by hours with nothing appearing to be broken.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import get_settings
from app.database import db_utc_offset


def reporting_tz() -> ZoneInfo | timezone:
    """The zone whose midnight starts a day. Falls back to the DB's own zone.

    A missing tz database would otherwise turn a statistics page into a 500;
    the DB offset is always available and is a defensible approximation.
    """
    name = get_settings().stats_timezone
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone(db_utc_offset())


def to_db_naive(moment: datetime) -> datetime:
    """Express an aware moment the way ``created_at`` stores it.

    The column holds naive local time on the DB server, so an aware value has to
    be moved into that zone and stripped before it can be compared.
    """
    return moment.astimezone(timezone(db_utc_offset())).replace(tzinfo=None)


def db_now() -> datetime:
    """Now, on the DB server's clock, naive — comparable with stored timestamps."""
    return datetime.now(timezone(db_utc_offset())).replace(tzinfo=None)


def week_start(day_start: datetime) -> datetime:
    """Monday 00:00 of the week containing ``day_start``.

    ``weekday()`` is 0 on Monday, so subtracting it lands on Monday and leaves a
    Monday untouched — the week is Monday–Sunday, matching how the team reports.
    """
    return day_start - timedelta(days=day_start.weekday())


def day_start_local(now_local: datetime) -> datetime:
    """Midnight of ``now_local``'s calendar day, in its own zone."""
    return now_local.replace(hour=0, minute=0, second=0, microsecond=0)


class Window:
    """A reporting window, carried in both forms so neither is recomputed.

    ``since``/``until`` are DB-local naive, for comparing with a ``created_at``
    column. ``since_local``/``until_local`` are the same two instants as aware
    values, for putting in a response — a timestamp handed to a browser without
    a zone is a timestamp the browser will guess about.
    """

    __slots__ = ("since", "until", "since_local", "until_local")

    def __init__(self, since_local: datetime, until_local: datetime) -> None:
        self.since_local = since_local
        self.until_local = until_local
        self.since = to_db_naive(since_local)
        self.until = to_db_naive(until_local)


def current_week(*, weeks_back: int = 0) -> Window:
    """The ISO week (Monday 00:00 → now) in the reporting zone.

    ``weeks_back=1`` gives the whole previous week, Monday 00:00 to the
    following Monday 00:00 — which is what a report written on Monday morning
    about "last week" actually wants. ``weeks_back=0`` ends at *now*, so it is
    small on a Monday and largest on Sunday night; callers showing it should say
    so rather than let a reader mistake a partial week for a quiet one.
    """
    tz = reporting_tz()
    now_local = datetime.now(tz)
    this_monday = week_start(day_start_local(now_local))
    if weeks_back <= 0:
        return Window(this_monday, now_local)
    since = this_monday - timedelta(weeks=weeks_back)
    return Window(since, since + timedelta(weeks=1))
