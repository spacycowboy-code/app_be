"""Skill comment / message board model: rp_ai_skill_comment."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.skill import RpAiSkill


class RpAiSkillComment(TimestampMixin, Base):
    __tablename__ = "rp_ai_skill_comment"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="CASCADE"), index=True
    )
    # Author EID (the commenter).
    author_eid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    skill: Mapped["RpAiSkill"] = relationship()
