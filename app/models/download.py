"""Skill download event log: rp_ai_skill_download.

One row per download of a skill's archive. Unlike the cumulative
``rp_ai_skill.downloads`` counter, this keeps a timestamped event per
download so dashboards (Grafana) can chart downloads over time
(e.g. weekly download volume).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RpAiSkillDownload(Base):
    __tablename__ = "rp_ai_skill_download"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # The downloaded skill. Kept as a plain indexed FK (ON DELETE CASCADE) so
    # deleting a skill also clears its download history.
    skill_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("rp_ai_skill.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Written by the DB in its local time zone (naive), matching every other
    # table's ``created_at``; indexed for time-range dashboard queries.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
