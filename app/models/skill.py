"""Skill model: rp_ai_skill, plus normalized child tables.

Mirrors the frontend SkillDetail interface. List-valued display fields
(tags, ratingDist) are stored as JSON; structured collections
(cases, subSkills, inputParams) are normalized into child tables.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.favorite import RpAiUserFavorite
    from app.models.review import RpAiSkillReview
    from app.models.user import RpAiPortal

# Skill review workflow states.
SKILL_STATUSES = ("pending", "approved", "rejected")


class RpAiSkill(TimestampMixin, Base):
    __tablename__ = "rp_ai_skill"

    # Frontend uses string ids (e.g. "tavily-web-search").
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    category: Mapped[str | None] = mapped_column(String(128))
    category_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("rp_ai_category.id", ondelete="SET NULL"), index=True
    )

    image: Mapped[str | None] = mapped_column(String(512))
    rating: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Frontend displays a formatted string like "5.6K".
    downloads: Mapped[str] = mapped_column(String(32), default="0", nullable=False)
    is_trending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_free: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tags: Mapped[list | None] = mapped_column(JSON)  # list[str]
    version: Mapped[str | None] = mapped_column(String(32))
    author: Mapped[str | None] = mapped_column(String(128))

    # Uploader EID -> rp_ai_portal.eid.
    uploader_eid: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("rp_ai_portal.eid", ondelete="SET NULL"), index=True
    )

    capability_full: Mapped[str | None] = mapped_column(Text)
    rating_dist: Mapped[list | None] = mapped_column(JSON)  # list[int], 5 buckets
    review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    example_prompt: Mapped[str | None] = mapped_column(Text)
    example_output: Mapped[str | None] = mapped_column(Text)
    # Raw skill definition / instructions (markdown).
    instructions: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)

    # Relationships.
    uploader: Mapped["RpAiPortal | None"] = relationship(
        back_populates="uploaded_skills",
        primaryjoin="RpAiSkill.uploader_eid == RpAiPortal.eid",
        viewonly=True,
    )
    cases: Mapped[list["RpAiSkillCase"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )
    sub_skills: Mapped[list["RpAiSubSkill"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )
    input_params: Mapped[list["RpAiInputParam"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["RpAiSkillReview"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )
    favorited_by: Mapped[list["RpAiUserFavorite"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )


class RpAiSkillCase(Base):
    __tablename__ = "rp_ai_skill_case"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    image: Mapped[str | None] = mapped_column(String(512))

    skill: Mapped["RpAiSkill"] = relationship(back_populates="cases")


class RpAiSubSkill(Base):
    __tablename__ = "rp_ai_sub_skill"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    badge: Mapped[str | None] = mapped_column(String(64))
    icon_color: Mapped[str | None] = mapped_column(String(32))
    icon_bg: Mapped[str | None] = mapped_column(String(32))

    skill: Mapped["RpAiSkill"] = relationship(back_populates="sub_skills")


class RpAiInputParam(Base):
    __tablename__ = "rp_ai_input_param"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str | None] = mapped_column(String(32))
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    skill: Mapped["RpAiSkill"] = relationship(back_populates="input_params")
