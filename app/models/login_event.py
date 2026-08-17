"""Portal sign-in event log: rp_ai_login_event.

One row per successful sign-in, so "how many people used the portal today /
this week" can be answered from evidence rather than guessed from a
last-login column. A single mutable ``last_login_at`` on the user row cannot
answer it: overwriting it loses every earlier day, so yesterday's active set
becomes unrecoverable the moment someone signs in again.

Rows are append-only and deliberately minimal — an EID and a timestamp. No
password, token, IP or user agent is stored: none of it is needed to count
active users, and a sign-in log is exactly the wrong place to accumulate data
nobody asked for.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RpAiLoginEvent(Base):
    __tablename__ = "rp_ai_login_event"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Signum of whoever signed in, uppercase (see services.eids.normalize_eid).
    # Not a ForeignKey to rp_ai_portal: the history of who signed in should
    # survive a user row being removed, and login writes must never fail
    # because of a missing parent row.
    eid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # Indexed: every query here is "distinct eids since <timestamp>".
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
