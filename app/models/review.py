"""Skill review model: rp_ai_skill_review.

Captures a multi-dimensional review (matching the frontend review
dimensions) plus a free-text comment. Reviews may be auto-generated.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.skill import RpAiSkill


class RpAiSkillReview(TimestampMixin, Base):
    __tablename__ = "rp_ai_skill_review"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="CASCADE"), index=True
    )
    # Reviewer EID; null for anonymous / automated reviews.
    reviewer_eid: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("rp_ai_portal.eid", ondelete="SET NULL"), index=True
    )

    # Review dimensions (0-5), matching the frontend review form.
    accuracy: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    value: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    docs: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    safety: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    overall: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    comment: Mapped[str | None] = mapped_column(Text)
    # True when produced by the automatic AI reviewer.
    is_auto: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    skill: Mapped["RpAiSkill"] = relationship(back_populates="reviews")
