"""User model: rp_ai_portal.

Stores a portal user keyed by EID, plus links to the skills they have
uploaded and the skills they have collected (favorited).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.favorite import RpAiUserFavorite
    from app.models.skill import RpAiSkill


class RpAiPortal(TimestampMixin, Base):
    __tablename__ = "rp_ai_portal"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Ericsson signum / EID — the natural key used by the frontend for auth.
    eid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    department: Mapped[str | None] = mapped_column(String(64))

    # Skills this user uploaded (RpAiSkill.uploader_eid -> rp_ai_portal.eid).
    uploaded_skills: Mapped[list["RpAiSkill"]] = relationship(
        back_populates="uploader",
        primaryjoin="RpAiPortal.eid == RpAiSkill.uploader_eid",
        viewonly=True,
    )
    # Skills this user collected/favorited.
    favorites: Mapped[list["RpAiUserFavorite"]] = relationship(
        back_populates="user",
        primaryjoin="RpAiPortal.eid == RpAiUserFavorite.eid",
        cascade="all, delete-orphan",
    )
