"""Category endpoints."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.category import RpAiCategory
from app.schemas.category import CategoryCreate, CategoryRead

router = APIRouter(prefix="/api/categories", tags=["categories"])


@router.get("", response_model=list[CategoryRead])
def list_categories(db: Session = Depends(get_db)) -> list[RpAiCategory]:
    return list(db.scalars(select(RpAiCategory)).all())


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
