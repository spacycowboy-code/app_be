"""Market item model: rp_ai_market_item.

Backs the Agent市场 (Tool & MCP Server & Agent). An item is either:
- a Code service (points at a git repo), or
- a Cloud service (points at a running web service URL),
and is typed as a Tool or an Agent. Items come from GitLab subgroup
syncs (source = gitlab-tools / gitlab-agents) or manual upload (manual).
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.market_version import RpAiMarketItemVersion

ITEM_TYPES = ("tool", "agent", "ai-platform")
SERVICE_TYPES = ("code", "cloud")
# ``upload`` = the package was uploaded through the portal, which created a
# GitLab project for it under the user-agents group. Such an item *is* a code
# service (it has a repo url like any other), so the new value belongs on this
# axis — how the item came to exist — and not on ``service_type``.
SOURCES = ("manual", "gitlab-tools", "gitlab-agents", "upload")

#: Lifecycle of an item. Only ``approved`` is visible to people other than the
#: uploader. Everything that predates the column is approved (see migrate.py):
#: the review gate exists for uploaded packages, and back-dating it onto items
#: that were already in the market would empty the market.
ITEM_STATUSES = ("pending", "approved", "rejected")


class RpAiMarketItem(TimestampMixin, Base):
    __tablename__ = "rp_ai_market_item"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # tool | agent | ai-platform
    item_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # code | cloud
    service_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # manual | gitlab-tools | gitlab-agents
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")

    # Code service -> git repository URL; Cloud service -> running service URL.
    repo_url: Mapped[str | None] = mapped_column(String(512))
    service_url: Mapped[str | None] = mapped_column(String(512))
    # Whether a cloud service may be shown via an embedded iframe preview.
    embeddable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Department / category (mirrors the skill categories).
    category_id: Mapped[str | None] = mapped_column(String(64), index=True)
    category: Mapped[str | None] = mapped_column(String(128))
    dept_color: Mapped[str | None] = mapped_column(String(16))

    tags: Mapped[list | None] = mapped_column(JSON)  # list[str]
    author: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[str | None] = mapped_column(String(32))
    rating: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    users: Mapped[str] = mapped_column(String(32), default="0", nullable=False)
    response_time: Mapped[str | None] = mapped_column(String(32))
    # ISO timestamp of the last update (GitLab last_activity_at for synced items).
    last_activity: Mapped[str | None] = mapped_column(String(40))
    is_trending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # EID of the user who manually created this item (None for GitLab-synced).
    creator: Mapped[str | None] = mapped_column(String(64))

    # --- review gate (uploaded packages) -----------------------------------
    # pending | approved | rejected. Uploaded packages start pending and are
    # hidden from everyone except their uploader until someone approves them;
    # every other path writes approved, which is what the market always was.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="approved", server_default="approved"
    )
    # Who approved/rejected it, and when. Kept because manual approval is
    # currently the *only* gate on an uploaded package: "who let this in" has
    # to be answerable without reading logs.
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)

    versions: Mapped[list["RpAiMarketItemVersion"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        order_by="RpAiMarketItemVersion.id",
    )
