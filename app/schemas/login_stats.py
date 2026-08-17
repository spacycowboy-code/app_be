"""Schemas for the sign-in statistics shown on the Dashboard."""
from __future__ import annotations

from datetime import datetime

from pydantic import field_serializer

from app.schemas.common import CamelModel, as_utc


class ActiveUser(CamelModel):
    """One person inside an active-user window."""

    eid: str
    display_name: str | None = None
    department: str | None = None
    #: Sign-ins by this person within the window. A session lasts hours, so this
    #: counts sign-ins, not visits — several in a day usually means a new
    #: browser or an expired session, not heavier use.
    logins: int
    last_login_at: datetime | None = None

    @field_serializer("last_login_at")
    def _utc(self, value: object) -> object:
        return as_utc(value)


class ActiveWindow(CamelModel):
    """Active users over one period, plus who they were.

    ``count`` is the number of distinct EIDs, i.e. what "日活 / 周活" means; it is
    always ``len(users)`` and is returned explicitly so a caller that only wants
    the number does not have to count the list.
    """

    since: datetime
    until: datetime
    count: int
    users: list[ActiveUser]

    @field_serializer("since", "until")
    def _utc(self, value: object) -> object:
        return as_utc(value)


class LoginStats(CamelModel):
    daily: ActiveWindow
    weekly: ActiveWindow
    #: Total sign-in events ever recorded — the log started when this feature
    #: shipped, so earlier activity is simply absent rather than zero.
    total_logins: int
    #: When the log begins, so a suspiciously low weekly number can be
    #: recognised as "we only started counting on this date".
    recording_since: datetime | None = None

    @field_serializer("recording_since")
    def _utc(self, value: object) -> object:
        return as_utc(value)
