"""Sign-in statistics: daily / weekly active users, and who they were.

Backs the Dashboard's active-user panel. Restricted to ``dashboard_eids``:
the numbers are aggregate, but the EID lists are "who used this portal and
when", which is not everyone's business.

Window definitions (documented because "日活/周活" can mean several things):

* **daily** — from midnight *today* to now, in ``stats_timezone`` (the users'
  zone, not the DB server's). A calendar day, not a rolling 24 hours, so it
  matches what people mean by "today" and resets at midnight.
* **weekly** — from **Monday** 00:00 of the current week to now (an ISO week,
  Monday–Sunday). A calendar week, not a rolling 7 days: it answers "how many
  people this week" the way a weekly report does, at the cost of being small on
  a Monday morning and largest on Sunday night. Use ``weekly.since`` to see
  which Monday the count starts from.

Both count **distinct EIDs**, so signing in five times counts once.

Time zones are the subtle part: ``created_at`` is written by the DB as a naive
value in the *DB server's* zone (UTC+2), while a day boundary only means
anything in the *viewer's* zone (UTC+8). Boundaries are therefore computed in
``stats_timezone`` and converted to DB-local naive values before they are
compared with the column; getting this wrong shifts the boundary by hours
without anything looking broken.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.login_event import RpAiLoginEvent
from app.models.user import RpAiPortal
from app.schemas.login_stats import ActiveUser, ActiveWindow, LoginStats
from app.services import reporting_windows

router = APIRouter(prefix="/api/login-stats", tags=["login-stats"])


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


# The window arithmetic lives in services.reporting_windows so that this page
# and the weekly usage report cannot drift into two different definitions of
# "this week". Re-exported under the original private names because that is how
# the tests (and any future caller) already reach them.
_reporting_tz = reporting_windows.reporting_tz
_to_db_naive = reporting_windows.to_db_naive
_db_now = reporting_windows.db_now
_week_start = reporting_windows.week_start


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
    week_start_local = _week_start(day_start_local)

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
