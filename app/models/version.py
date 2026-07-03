"""Skill version history model: rp_ai_skill_version.

One row per upload of a skill. The git repo holds the actual content of
the latest version; this table records the version lineage (what version,
which commit/tag, who uploaded it, when).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.skill import RpAiSkill


class RpAiSkillVersion(TimestampMixin, Base):
    __tablename__ = "rp_ai_skill_version"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    # Git commit sha and tag that captured this version.
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    tag: Mapped[str | None] = mapped_column(String(128))
    # Who uploaded this version.
    uploader_eid: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("rp_ai_portal.eid", ondelete="SET NULL"), index=True
    )
    # Uploaded artifact filename (stored under the skill's artifacts/ dir).
    artifact_name: Mapped[str | None] = mapped_column(String(256))
    notes: Mapped[str | None] = mapped_column(Text)

    skill: Mapped["RpAiSkill"] = relationship(back_populates="versions")
