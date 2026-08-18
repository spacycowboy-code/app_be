"""Review endpoints nested under a skill."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.review import RpAiSkillReview
from app.models.skill import RpAiSkill
from app.schemas.review import ReviewCreate, ReviewRead
from app.services.eids import normalize_eid

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
        reviewer_eid=normalize_eid(payload.reviewer_eid),
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
    #
    # Written as a Core UPDATE that names ``updated_at`` so the column keeps its
    # current value: ``TimestampMixin.updated_at`` carries ``onupdate=func.now()``,
    # which fires on any UPDATE that omits it, and the Home page's "What's New"
    # feed treats ``updated_at > created_at`` as "this skill was updated". Someone
    # else rating a skill is not the author shipping a new version, so it must not
    # surface as an "Update" card (the skill's ``version`` stays put either way).
    prev_count = skill.review_count or 0
    new_count = prev_count + 1
    new_rating = round(((skill.rating * prev_count) + overall) / new_count, 1)
    db.execute(
        update(RpAiSkill)
        .where(RpAiSkill.id == skill.id)
        .values(
            rating=new_rating,
            review_count=new_count,
            updated_at=RpAiSkill.updated_at,
        )
    )
    # The Core UPDATE bypassed the ORM; drop the now-stale attributes so any
    # later read in this session sees the stored values.
    db.expire(skill, ["rating", "review_count", "updated_at"])

    db.commit()
    db.refresh(review)
    return review
