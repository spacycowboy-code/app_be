"""Category endpoints."""
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.category import RpAiCategory
from app.models.skill import RpAiSkill
from app.schemas.category import CategoryCreate, CategoryRead
from app.services.eids import acting_eid

router = APIRouter(prefix="/api/categories", tags=["categories"])


@router.get("", response_model=list[CategoryRead])
def list_categories(
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> list[CategoryRead]:
    """Return categories with live skill counts ('all' = total).

    The counts apply the same visibility rule as ``GET /api/skills`` — a skill
    awaiting review is not public, admin sees everything, an uploader still sees
    their own. They have to agree: the frontend uses a count of zero to decide
    the group filter would show an empty page, and a count that includes skills
    the list then withholds would send someone to a blank market.
    """
    settings = get_settings()
    categories = list(db.scalars(select(RpAiCategory)))

    visible = select(RpAiSkill.category_id, func.count(RpAiSkill.id)).group_by(
        RpAiSkill.category_id
    )
    requester = acting_eid(request, x_user_eid)
    if requester != settings.audit_admin_eid:
        visible = visible.where(
            (RpAiSkill.status == "approved") | (RpAiSkill.uploader_eid == requester)
        )
    counts = dict(db.execute(visible).all())
    total = sum(counts.values())

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
