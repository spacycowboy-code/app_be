"""User endpoints: register/lookup portal users and list their skills."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.favorite import RpAiUserFavorite
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.schemas.favorite import FavoriteRead
from app.schemas.skill import SkillSummary
from app.schemas.user import UserCreate, UserRead, UserUpdate

router = APIRouter(prefix="/api/users", tags=["users"])


def _get_user_or_404(db: Session, eid: str) -> RpAiPortal:
    user = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User '{eid}' not found")
    return user


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> RpAiPortal:
    """Create a user, or return the existing one (idempotent by EID)."""
    existing = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == payload.eid))
    if existing:
        return existing
    user = RpAiPortal(**payload.model_dump())
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("", response_model=list[UserRead])
def list_users(db: Session = Depends(get_db)) -> list[RpAiPortal]:
    return list(db.scalars(select(RpAiPortal)).all())


@router.get("/{eid}", response_model=UserRead)
def get_user(eid: str, db: Session = Depends(get_db)) -> RpAiPortal:
    return _get_user_or_404(db, eid)


@router.patch("/{eid}", response_model=UserRead)
def update_user(
    eid: str, payload: UserUpdate, db: Session = Depends(get_db)
) -> RpAiPortal:
    user = _get_user_or_404(db, eid)
    for key, val in payload.model_dump(exclude_unset=True).items():
        setattr(user, key, val)
    db.commit()
    db.refresh(user)
    return user


@router.get("/{eid}/skills", response_model=list[SkillSummary])
def list_uploaded_skills(eid: str, db: Session = Depends(get_db)) -> list[RpAiSkill]:
    """Skills uploaded by this user."""
    _get_user_or_404(db, eid)
    return list(
        db.scalars(select(RpAiSkill).where(RpAiSkill.uploader_eid == eid)).all()
    )


@router.get("/{eid}/favorites", response_model=list[FavoriteRead])
def list_favorites(eid: str, db: Session = Depends(get_db)) -> list[RpAiUserFavorite]:
    """Skills this user has collected."""
    _get_user_or_404(db, eid)
    return list(
        db.scalars(
            select(RpAiUserFavorite).where(RpAiUserFavorite.eid == eid)
        ).all()
    )
