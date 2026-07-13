"""Market favorite (collection) endpoints for a given user EID.

Mirrors the skill favorites router but for market items (tools / agents).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.market import RpAiMarketItem
from app.models.market_favorite import RpAiMarketFavorite
from app.models.user import RpAiPortal
from app.schemas.market import MarketItemRead
from app.schemas.market_favorite import MarketFavoriteCreate
from app.services.eids import normalize_eid

router = APIRouter(prefix="/api/users/{eid}/market-favorites", tags=["market-favorites"])


def _ensure_user(db: Session, eid: str) -> None:
    """Create the user row on demand (favoriting shouldn't 404 on first use)."""
    if db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid)) is None:
        db.add(RpAiPortal(eid=eid))
        db.flush()


@router.get("", response_model=list[MarketItemRead])
def list_market_favorites(eid: str, db: Session = Depends(get_db)) -> list[RpAiMarketItem]:
    """Tools / agents this user has collected."""
    eid = normalize_eid(eid)
    stmt = (
        select(RpAiMarketItem)
        .join(RpAiMarketFavorite, RpAiMarketFavorite.item_id == RpAiMarketItem.id)
        .where(RpAiMarketFavorite.eid == eid)
        .order_by(RpAiMarketItem.name)
    )
    return list(db.scalars(stmt).all())


@router.post("", status_code=status.HTTP_201_CREATED)
def add_market_favorite(
    eid: str, payload: MarketFavoriteCreate, db: Session = Depends(get_db)
) -> dict:
    eid = normalize_eid(eid)
    _ensure_user(db, eid)
    if db.get(RpAiMarketItem, payload.item_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Market item '{payload.item_id}' not found"
        )
    existing = db.scalar(
        select(RpAiMarketFavorite).where(
            RpAiMarketFavorite.eid == eid,
            RpAiMarketFavorite.item_id == payload.item_id,
        )
    )
    if existing is None:
        db.add(RpAiMarketFavorite(eid=eid, item_id=payload.item_id))
        db.commit()
    return {"eid": eid, "itemId": payload.item_id}


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_market_favorite(eid: str, item_id: str, db: Session = Depends(get_db)) -> None:
    eid = normalize_eid(eid)
    fav = db.scalar(
        select(RpAiMarketFavorite).where(
            RpAiMarketFavorite.eid == eid, RpAiMarketFavorite.item_id == item_id
        )
    )
    if fav is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Favorite not found")
    db.delete(fav)
    db.commit()
