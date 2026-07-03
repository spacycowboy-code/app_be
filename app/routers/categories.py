"""Category endpoints."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.category import RpAiCategory
from app.models.skill import RpAiSkill
from app.schemas.category import CategoryCreate, CategoryRead

router = APIRouter(prefix="/api/categories", tags=["categories"])


@router.get("", response_model=list[CategoryRead])
def list_categories(db: Session = Depends(get_db)) -> list[CategoryRead]:
    """Return categories with live skill counts ('all' = total)."""
    categories = list(db.scalars(select(RpAiCategory)))

    counts = dict(
        db.execute(
            select(RpAiSkill.category_id, func.count(RpAiSkill.id)).group_by(
                RpAiSkill.category_id
            )
        ).all()
    )
    total = db.scalar(select(func.count()).select_from(RpAiSkill)) or 0

    result: list[CategoryRead] = []
    for cat in categories:
        n = total if cat.id == "all" else int(counts.get(cat.id, 0))
        result.append(CategoryRead(id=cat.id, label=cat.label, count=n))
    return result


@router.post("", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate, db: Session = Depends(get_db)
) -> RpAiCategory:
    if db.get(RpAiCategory, payload.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Category '{payload.id}' already exists"
        )
    category = RpAiCategory(**payload.model_dump())
    db.add(category)
    db.commit()
    db.refresh(category)
    return category
