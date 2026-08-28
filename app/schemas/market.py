"""Schemas for market items (rp_ai_market_item)."""
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.card_group import CardGroupRead
from app.schemas.common import CamelModel

ItemType = Literal["tool", "agent", "ai-platform"]
ServiceType = Literal["code", "cloud"]


class MarketItemCreate(CamelModel):
    name: str
    description: str | None = None
    item_type: ItemType
    service_type: ServiceType
    repo_url: str | None = None
    service_url: str | None = None
    embeddable: bool = False
    category_id: str | None = None
    category: str | None = None
    dept_color: str | None = None
    tags: list[str] = Field(default_factory=list)
    author: str | None = None
    version: str | None = "1.0"

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v: object) -> object:
        return v if v is not None else []


class MarketItemRead(CamelModel):
    id: str
    name: str
    description: str | None = None
    item_type: ItemType
    service_type: ServiceType
    source: str
    repo_url: str | None = None
    service_url: str | None = None
    embeddable: bool = False
    category_id: str | None = None
    category: str | None = None
    dept_color: str | None = None
    tags: list[str] = Field(default_factory=list)
    author: str | None = None
    version: str | None = None
    rating: float = 0.0
    users: str = "0"
    response_time: str | None = None
    last_activity: str | None = None
    is_trending: bool = False
    verified: bool = False
    creator: str | None = None
    # pending | approved | rejected. Only approved items are visible to others;
    # the field is on the read model so the uploader's own card can say so.
    status: str = "approved"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    # Deduplicated-by-EID stats shown on the card.
    favorite_count: int = 0
    download_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Where the creator sits: 地址 · 大部门 · 小部门. None for GitLab-synced items,
    # which have no creator at all, and for a creator outside Radio Products.
    group: CardGroupRead | None = None


class MarketItemVersionRead(CamelModel):
    """One uploaded version of an agent package."""

    version: str
    commit_sha: str | None = None
    tag: str | None = None
    uploader_eid: str | None = None
    artifact_name: str | None = None
    artifact_bytes: int | None = None
    metadata_json: dict | None = None
    audit_json: dict | None = None
    created_at: datetime | None = None


class MarketReviewDecision(CamelModel):
    """An approve/reject decision. Deliberately not free-form."""

    status: Literal["approved", "rejected"]
    note: str | None = None


class MarketReviewRead(CamelModel):
    """Everything the review page needs, in one request.

    Assembled server-side because the page's whole purpose is a judgement call:
    a reviewer who has to click through three screens to see the file list will
    approve without looking. Manual approval is currently the only gate, so the
    facts have to be in front of them.
    """

    item: MarketItemRead
    versions: list[MarketItemVersionRead] = Field(default_factory=list)
    # Files as they exist in GitLab right now (not as recorded at upload time).
    files: list[dict] = Field(default_factory=list)
    files_error: str | None = None
    compare_url: str | None = None
    project_url: str | None = None
    uploader_name: str | None = None


class MarketSyncResult(CamelModel):
    synced: int
    created: int
    updated: int
    groups: list[str] = Field(default_factory=list)
    message: str | None = None
