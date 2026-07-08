"""Favorite (collection) endpoints for a given user EID."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.favorite import RpAiUserFavorite
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.schemas.favorite import FavoriteCreate, FavoriteRead
from app.services.eids import normalize_eid

router = APIRouter(prefix="/api/users/{eid}/favorites", tags=["favorites"])


def _ensure_user(db: Session, eid: str) -> None:
    if db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User '{eid}' not found")


@router.post("", response_model=FavoriteRead, status_code=status.HTTP_201_CREATED)
def add_favorite(
    eid: str, payload: FavoriteCreate, db: Session = Depends(get_db)
) -> RpAiUserFavorite:
    eid = normalize_eid(eid)
    _ensure_user(db, eid)
    if db.get(RpAiSkill, payload.skill_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Skill '{payload.skill_id}' not found"
        )
    existing = db.scalar(
        select(RpAiUserFavorite).where(
            RpAiUserFavorite.eid == eid,
            RpAiUserFavorite.skill_id == payload.skill_id,
        )
    )
    if existing:
        return existing
    fav = RpAiUserFavorite(eid=eid, skill_id=payload.skill_id)
    db.add(fav)
    db.commit()
    db.refresh(fav)
    return fav


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_favorite(eid: str, skill_id: str, db: Session = Depends(get_db)) -> None:
    fav = db.scalar(
        select(RpAiUserFavorite).where(
            RpAiUserFavorite.eid == eid, RpAiUserFavorite.skill_id == skill_id
        )
    )
    if fav is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Favorite not found")
    db.delete(fav)
    db.commit()
