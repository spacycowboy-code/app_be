"""Helpers shared by everything that records what a user did.

The one thing worth centralising is the department snapshot. Every event table
stores the actor's department **as it was**, because a person moves teams and
the department→group table itself changes (which is also why the frontend's
``GroupService`` refuses to put the group in a JWT claim). If a report joined
today's department to last quarter's download, a reorganisation would silently
rewrite last quarter's numbers — and the report would still look plausible,
which is the dangerous part.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import RpAiPortal


def department_of(db: Session, eid: str | None) -> str | None:
    """The department currently on the user's row, for storing alongside an event.

    Returns ``None`` for an unknown EID rather than raising: recording is a
    side effect of some action the user asked for, and it must never be the
    reason that action fails.
    """
    if not eid:
        return None
    return db.scalar(select(RpAiPortal.department).where(RpAiPortal.eid == eid))
