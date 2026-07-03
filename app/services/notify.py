"""Generate workspace activities (notifications).

Callers add the activity rows within their own transaction and commit.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.activity import ACTIVITY_COMMENT, ACTIVITY_VERSION, RpAiActivity
from app.models.favorite import RpAiUserFavorite
from app.models.skill import RpAiSkill


def notify_comment(db: Session, skill: RpAiSkill, actor_eid: str, content: str) -> None:
    """Notify the skill owner that someone left a comment."""
    owner = skill.uploader_eid
    if not owner or owner == actor_eid:
        return
    snippet = content.strip().replace("\n", " ")
    if len(snippet) > 40:
        snippet = snippet[:40] + "…"
    db.add(
        RpAiActivity(
            recipient_eid=owner,
            type=ACTIVITY_COMMENT,
            skill_id=skill.id,
            skill_name=skill.name,
            actor_eid=actor_eid,
            message=f"{actor_eid} 给你的 Skill「{skill.name}」留言：{snippet}",
        )
    )


def notify_version_update(
    db: Session, skill: RpAiSkill, version: str, actor_eid: str | None = None
) -> None:
    """Notify everyone who favorited the skill that a new version shipped."""
    favoriters = db.scalars(
        select(RpAiUserFavorite.eid).where(RpAiUserFavorite.skill_id == skill.id)
    ).all()
    for eid in set(favoriters):
        if eid == actor_eid:
            continue
        db.add(
            RpAiActivity(
                recipient_eid=eid,
                type=ACTIVITY_VERSION,
                skill_id=skill.id,
                skill_name=skill.name,
                actor_eid=actor_eid,
                version=version,
                message=f"你收藏的 Skill「{skill.name}」更新到 v{version}",
            )
        )
