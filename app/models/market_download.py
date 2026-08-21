"""Market item (tool/agent) download event log: rp_ai_market_download.

One row per download of a market item's repo archive. Mirrors
``RpAiSkillDownload`` (skill-only): a timestamped, EID-tagged event so the
Agent市场 can show a unique-downloader (deduplicated by EID) count and
dashboards can chart downloads over time.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RpAiMarketDownload(Base):
    __tablename__ = "rp_ai_market_download"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # The downloaded market item (ON DELETE CASCADE clears history with the item).
    item_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("rp_ai_market_item.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # EID of the downloader when known; indexed for per-user / dedup counts.
    eid: Mapped[str | None] = mapped_column(String(64), index=True)
    # Department as it was at the time — see RpAiSkillDownload for why this is a
    # snapshot rather than a join.
    department: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
