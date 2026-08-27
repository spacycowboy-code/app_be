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
    from app.models.pending_update import RpAiSkillPendingUpdate
    from app.models.review import RpAiSkillReview
    from app.models.user import RpAiPortal
    from app.models.version import RpAiSkillVersion

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

    # --- storage (git repo) -------------------------------------------------
    # Relative path within the skill repo, e.g. "public/tavily_ezhgmio_Tavily".
    storage_path: Mapped[str | None] = mapped_column(String(512))
    # Latest git commit sha that stored this skill.
    repo_commit: Mapped[str | None] = mapped_column(String(64))
    # Security-audit derived state: pass | suspect (danger is never stored).
    audit_state: Mapped[str | None] = mapped_column(String(16), index=True)
    # 描述**当前发布内容**的那条审计记录(``rp_ai_skill_audit_record.id``)。
    #
    # 收敛器需要"当前这一版的结论是哪一条"这个答案,而它没法从时间戳推:记录的 created_at 只精确
    # 到秒(MySQL DATETIME 默认如此),同一秒里落下的两条(上传完紧接着编辑一次)谁在后是不确定的,
    # 而 id 是随机 uuid,拿它做次级排序等于抛硬币。所以这里把答案记下来,而不是每次去猜。
    # 老数据是 NULL,收敛器对它们仍按时间倒序取最新一条。
    audit_ref: Mapped[str | None] = mapped_column(String(32))

    @property
    def uploader_name(self) -> str | None:
        """The uploader's LDAP display name (from the linked user row).

        ``author`` stores the uploader's EID (identity); the human-readable
        display name lives on the user record and is resolved here for read
        models so the UI can show e.g. "Miao Zhang M".
        """
        up = self.uploader
        return up.display_name if up else None

    @property
    def creator_label(self) -> str | None:
        """The name to show as the skill's creator.

        For user uploads ``author`` holds the uploader's EID, so show the
        person's LDAP display name (falling back to the EID). For imported
        skills ``author`` is a real label (e.g. "Anthropic") that differs from
        the uploader EID, so show it verbatim.
        """
        if self.author and self.author != self.uploader_eid:
            return self.author
        return self.uploader_name or self.uploader_eid

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
    versions: Mapped[list["RpAiSkillVersion"]] = relationship(
        back_populates="skill",
        cascade="all, delete-orphan",
        order_by="RpAiSkillVersion.id",
    )
    # 压在线上这一版之外的、正在人工复核的更新(最多一份)。它存在不影响这一行的任何字段:
    # 这一行始终描述**已发布**的内容。见 models/pending_update.py。
    pending_update: Mapped["RpAiSkillPendingUpdate | None"] = relationship(
        back_populates="skill", cascade="all, delete-orphan", uselist=False
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
