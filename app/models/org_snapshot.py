"""Org snapshot model: rp_ai_org_snapshot.

Holds the Microsoft Graph reporting tree the department report counts people by.

In the database rather than in a file on the host because the report has to work
in a deployed container, and a file there means a mounted volume, a copy step at
release time, and a report that starts answering 503 the first time somebody
redeploys without remembering it. One row, replaced when a fresh walk is pushed.

Not derived from anything the portal already stores: ``rp_ai_portal.department``
is free text copied at sign-in and only exists for people who have signed in, so
it cannot say how many people a department has.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RpAiOrgSnapshot(Base):
    __tablename__ = "rp_ai_org_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # The whole Graph walk, as fetched. Kept as the tool produced it so the parsing
    # rules can change without needing a new walk — and so a disagreement with the
    # org chart can be settled by looking at what the directory actually said.
    #
    # MEDIUMTEXT on MySQL: the real payload is around 450 kB and TEXT stops at 64,
    # which would truncate silently and leave a tree that parses but is missing
    # people.
    payload: Mapped[str] = mapped_column(
        Text().with_variant(MEDIUMTEXT, "mysql"), nullable=False
    )
    #: When the directory was read, as the tool recorded it — not when it was
    #: uploaded. The report prints this, and the reader needs the age of the data.
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    #: Who pushed it, so a stale or surprising org structure has someone to ask.
    uploaded_by: Mapped[str | None] = mapped_column(String(64))
    #: Number of people in the walk. Stored alongside so the row can be sanity
    #: checked without parsing half a megabyte of JSON.
    people: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
