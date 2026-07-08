"""Skill comment endpoints (message board on the skill detail page)."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.comment import RpAiSkillComment
from app.models.skill import RpAiSkill
from app.schemas.social import CommentCreate, CommentRead
from app.services.notify import notify_comment
from app.services.eids import normalize_eid

router = APIRouter(prefix="/api/skills/{skill_id}/comments", tags=["comments"])


def _get_skill_or_404(db: Session, skill_id: str) -> RpAiSkill:
    skill = db.get(RpAiSkill, skill_id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Skill '{skill_id}' not found")
    return skill


@router.get("", response_model=list[CommentRead])
def list_comments(skill_id: str, db: Session = Depends(get_db)) -> list[RpAiSkillComment]:
    _get_skill_or_404(db, skill_id)
    return list(
        db.scalars(
            select(RpAiSkillComment)
            .where(RpAiSkillComment.skill_id == skill_id)
            .order_by(RpAiSkillComment.id.desc())
        ).all()
    )


@router.post("", response_model=CommentRead, status_code=status.HTTP_201_CREATED)
def create_comment(
    skill_id: str, payload: CommentCreate, db: Session = Depends(get_db)
) -> RpAiSkillComment:
    skill = _get_skill_or_404(db, skill_id)
    content = payload.content.strip()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "留言内容不能为空")

    comment = RpAiSkillComment(
        skill_id=skill_id, author_eid=normalize_eid(payload.author_eid), content=content
    )
    db.add(comment)
    # Notify the skill owner.
    notify_comment(db, skill, normalize_eid(payload.author_eid), content)
    db.commit()
    db.refresh(comment)
    return comment
