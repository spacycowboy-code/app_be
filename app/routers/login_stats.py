"""Sign-in statistics: daily / weekly active users, and who they were.

Backs the Dashboard's active-user panel. Restricted to ``dashboard_eids``:
the numbers are aggregate, but the EID lists are "who used this portal and
when", which is not everyone's business.

Window definitions (documented because "日活/周活" can mean several things):

* **daily** — from midnight *today* to now, in ``stats_timezone`` (the users'
  zone, not the DB server's). A calendar day, not a rolling 24 hours, so it
  matches what people mean by "today" and resets at midnight.
* **weekly** — a rolling 7 calendar days ending today (today plus the previous
  six). Rolling rather than "since Monday" so the number is comparable on any
  day of the week instead of being near-zero every Monday morning.

Both count **distinct EIDs**, so signing in five times counts once.

Time zones are the subtle part: ``created_at`` is written by the DB as a naive
value in the *DB server's* zone (UTC+2), while a day boundary only means
anything in the *viewer's* zone (UTC+8). Boundaries are therefore computed in
``stats_timezone`` and converted to DB-local naive values before they are
compared with the column; getting this wrong shifts the boundary by hours
without anything looking broken.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import db_utc_offset, get_db
from app.models.login_event import RpAiLoginEvent
from app.models.user import RpAiPortal
from app.schemas.login_stats import ActiveUser, ActiveWindow, LoginStats

router = APIRouter(prefix="/api/login-stats", tags=["login-stats"])

#: A rolling week: today plus the previous six calendar days.
WEEK_DAYS = 7


def _require_dashboard_access(request: Request) -> str:
    """Allow only the configured EIDs; 403 for any other signed-in user.

    The site-wide gate in ``app.main`` has already rejected anonymous callers,
    so reaching here means a valid session — just not necessarily an allowed
    one. Hiding the Dashboard link in the sidebar is a UI convenience; this is
    the check that actually keeps the EID lists private.
    """
    eid = str(getattr(request.state, "eid", "") or "").strip().upper()
    if not eid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    if eid not in get_settings().dashboard_eid_list:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You are not allowed to view sign-in statistics."
        )
    return eid


def _reporting_tz() -> ZoneInfo | timezone:
    """The zone whose midnight starts a day. Falls back to the DB's own zone.

    A missing tz database would otherwise turn a statistics page into a 500;
    the DB offset is always available and is a defensible approximation.
    """
    name = get_settings().stats_timezone
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone(db_utc_offset())


def _to_db_naive(moment: datetime) -> datetime:
    """Express an aware moment the way ``created_at`` stores it.

    The column holds naive local time on the DB server, so an aware value has to
    be moved into that zone and stripped before it can be compared.
    """
    return moment.astimezone(timezone(db_utc_offset())).replace(tzinfo=None)


def _db_now() -> datetime:
    """Now, on the DB server's clock, naive — comparable with stored timestamps."""
    return datetime.now(timezone(db_utc_offset())).replace(tzinfo=None)


def _window(
    db: Session, since: datetime, until: datetime, *, since_out: datetime, until_out: datetime
) -> ActiveWindow:
    """Distinct people who signed in within ``[since, until]``, newest first.

    ``since``/``until`` are DB-local naive (for the query); ``*_out`` are the
    same instants as aware values (for the response), so the browser is never
    handed an unlabelled timestamp.
    """
    rows = db.execute(
        select(
            RpAiLoginEvent.eid,
            func.count(RpAiLoginEvent.id).label("logins"),
            func.max(RpAiLoginEvent.created_at).label("last_login_at"),
        )
        .where(RpAiLoginEvent.created_at >= since, RpAiLoginEvent.created_at <= until)
        .group_by(RpAiLoginEvent.eid)
    ).all()

    # One extra query for display names rather than a join, so a login event
    # whose user row was removed still appears (with just its EID) instead of
    # vanishing from the count.
    names: dict[str, RpAiPortal] = {}
    if rows:
        users = db.scalars(
            select(RpAiPortal).where(RpAiPortal.eid.in_([r.eid for r in rows]))
        ).all()
        names = {u.eid: u for u in users}

    users_out = [
        ActiveUser(
            eid=row.eid,
            display_name=names[row.eid].display_name if row.eid in names else None,
            department=names[row.eid].department if row.eid in names else None,
            logins=int(row.logins or 0),
            last_login_at=row.last_login_at,
        )
        for row in rows
    ]
    # Most recent sign-in first: on a dashboard, "who is here now" is the
    # question being asked, and alphabetical order answers a different one.
    users_out.sort(key=lambda u: (u.last_login_at or since), reverse=True)

    return ActiveWindow(
        since=since_out, until=until_out, count=len(users_out), users=users_out
    )


@router.get("", response_model=LoginStats)
def login_stats(
    request: Request, db: Session = Depends(get_db), _: str = Depends(_require_dashboard_access)
) -> LoginStats:
    """Daily and weekly active users, with the signums behind each number."""
    tz = _reporting_tz()
    now_local = datetime.now(tz)
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start_local = day_start_local - timedelta(days=WEEK_DAYS - 1)

    now_db = _db_now()
    total = db.scalar(select(func.count(RpAiLoginEvent.id))) or 0
    first_seen = db.scalar(select(func.min(RpAiLoginEvent.created_at)))

    return LoginStats(
        daily=_window(
            db,
            _to_db_naive(day_start_local),
            now_db,
            since_out=day_start_local,
            until_out=now_local,
        ),
        weekly=_window(
            db,
            _to_db_naive(week_start_local),
            now_db,
            since_out=week_start_local,
            until_out=now_local,
        ),
        total_logins=int(total),
        recording_since=first_seen,
    )
