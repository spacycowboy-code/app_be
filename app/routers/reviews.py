"""Review endpoints nested under a skill."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.review import RpAiSkillReview
from app.models.skill import RpAiSkill
from app.schemas.review import ReviewCreate, ReviewRead

router = APIRouter(prefix="/api/skills/{skill_id}/reviews", tags=["reviews"])


def _get_skill_or_404(db: Session, skill_id: str) -> RpAiSkill:
    skill = db.get(RpAiSkill, skill_id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Skill '{skill_id}' not found")
    return skill


@router.get("", response_model=list[ReviewRead])
def list_reviews(skill_id: str, db: Session = Depends(get_db)) -> list[RpAiSkillReview]:
    _get_skill_or_404(db, skill_id)
    return list(
        db.scalars(
            select(RpAiSkillReview).where(RpAiSkillReview.skill_id == skill_id)
        ).all()
    )


@router.post("", response_model=ReviewRead, status_code=status.HTTP_201_CREATED)
def create_review(
    skill_id: str, payload: ReviewCreate, db: Session = Depends(get_db)
) -> RpAiSkillReview:
    skill = _get_skill_or_404(db, skill_id)

    dims = [payload.accuracy, payload.value, payload.docs, payload.safety]
    overall = round(sum(dims) / len(dims), 1)

    review = RpAiSkillReview(
        skill_id=skill_id,
        reviewer_eid=payload.reviewer_eid,
        accuracy=payload.accuracy,
        value=payload.value,
        docs=payload.docs,
        safety=payload.safety,
        overall=overall,
        comment=payload.comment,
        is_auto=payload.is_auto,
    )
    db.add(review)

    # Recompute the skill's aggregate rating and review count.
    prev_count = skill.review_count or 0
    new_count = prev_count + 1
    skill.rating = round(((skill.rating * prev_count) + overall) / new_count, 1)
    skill.review_count = new_count

    db.commit()
    db.refresh(review)
    return review
