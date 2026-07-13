"""Market favorite / collection model: rp_ai_market_favorite.

Links a user (by EID) to a market item (tool / agent) they have collected.
Mirrors ``RpAiUserFavorite`` (which is skill-only) for the Agent市场.
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin


class RpAiMarketFavorite(TimestampMixin, Base):
    __tablename__ = "rp_ai_market_favorite"
    __table_args__ = (
        UniqueConstraint("eid", "item_id", name="uq_mfav_eid_item"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    eid: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_portal.eid", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("rp_ai_market_item.id", ondelete="CASCADE"),
        index=True,
    )
