"""User favorite / collection model: rp_ai_user_favorite.

Links a user (by EID) to a skill they have collected.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.skill import RpAiSkill
    from app.models.user import RpAiPortal


class RpAiUserFavorite(TimestampMixin, Base):
    __tablename__ = "rp_ai_user_favorite"
    __table_args__ = (
        UniqueConstraint("eid", "skill_id", name="uq_favorite_eid_skill"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    eid: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_portal.eid", ondelete="CASCADE"), index=True
    )
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="CASCADE"), index=True
    )

    user: Mapped["RpAiPortal"] = relationship(
        back_populates="favorites",
        primaryjoin="RpAiUserFavorite.eid == RpAiPortal.eid",
    )
    skill: Mapped["RpAiSkill"] = relationship(back_populates="favorited_by")
