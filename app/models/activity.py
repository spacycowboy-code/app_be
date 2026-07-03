"""Workspace activity feed model: rp_ai_activity.

One row per notification directed at a recipient user, e.g. "someone
commented on your skill" or "a skill you favorited was updated".
"""
from __future__ import annotations

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin

# Activity types.
ACTIVITY_COMMENT = "comment"
ACTIVITY_VERSION = "version_update"


class RpAiActivity(TimestampMixin, Base):
    __tablename__ = "rp_ai_activity"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # The user who should see this activity in their workspace.
    recipient_eid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Denormalized fields for easy display.
    skill_id: Mapped[str | None] = mapped_column(String(64))
    skill_name: Mapped[str | None] = mapped_column(String(200))
    actor_eid: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
