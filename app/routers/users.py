"""User endpoints: register/lookup portal users and list their skills."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.activity import RpAiActivity
from app.models.favorite import RpAiUserFavorite
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.schemas.favorite import FavoriteRead
from app.schemas.skill import SkillSummary
from app.services.asset_stats import attach_skill_stats
from app.schemas.social import ActivityRead
from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.services.eids import normalize_eid

router = APIRouter(prefix="/api/users", tags=["users"])


def _get_user_or_404(db: Session, eid: str) -> RpAiPortal:
    user = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User '{eid}' not found")
    return user


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> RpAiPortal:
    """Create a user, or return the existing one (idempotent by EID)."""
    data = payload.model_dump()
    data["eid"] = normalize_eid(data.get("eid"))
    existing = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == data["eid"]))
    if existing:
        return existing
    user = RpAiPortal(**data)
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
    skills = list(
        db.scalars(select(RpAiSkill).where(RpAiSkill.uploader_eid == eid)).all()
    )
    # 这条端点发的是 SkillSummary(带 downloads / 点赞 / 组),此前直接返回 ORM 行,于是同一个
    # 技能在"我上传的"列表里显示的下载量和市场卡片不一样。统计一律走 attach_skill_stats。
    attach_skill_stats(db, skills)
    return skills


@router.get("/{eid}/favorites", response_model=list[FavoriteRead])
def list_favorites(eid: str, db: Session = Depends(get_db)) -> list[RpAiUserFavorite]:
    """Skills this user has collected."""
    _get_user_or_404(db, eid)
    return list(
        db.scalars(
            select(RpAiUserFavorite).where(RpAiUserFavorite.eid == eid)
        ).all()
    )


@router.get("/{eid}/activities", response_model=list[ActivityRead])
def list_activities(
    eid: str, limit: int = 30, db: Session = Depends(get_db)
) -> list[RpAiActivity]:
    """Workspace activity feed for this user (most recent first)."""
    return list(
        db.scalars(
            select(RpAiActivity)
            .where(RpAiActivity.recipient_eid == eid)
            .order_by(RpAiActivity.id.desc())
            .limit(limit)
        ).all()
    )
