"""Pydantic schemas for market favorites (rp_ai_market_favorite)."""
from app.schemas.common import CamelModel


class MarketFavoriteCreate(CamelModel):
    item_id: str
