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
from app.services.portal_users import ensure_portal_user

router = APIRouter(prefix="/api/users/{eid}/market-favorites", tags=["market-favorites"])


def _ensure_user(db: Session, eid: str) -> None:
    """建行时一并写上 department（收藏）。

    这里原本是 ``RpAiPortal(eid=eid)`` 只写工号。报告与 Department Details 都按
    department 归属，列是空的就等于这个人不属于任何部门、名下资产落进「无法归属」——
    而这不报错：行建出来了、动作成功了、只是那个人从部门统计里消失了。
    2026-09-07 在 ``ivc_sync`` 修过同一个形状，代价是 5 个人 10 件技能。
    """
    ensure_portal_user(db, eid)


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
