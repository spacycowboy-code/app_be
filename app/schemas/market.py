"""Schemas for market items (rp_ai_market_item)."""
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

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
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MarketSyncResult(CamelModel):
    synced: int
    created: int
    updated: int
    groups: list[str] = Field(default_factory=list)
    message: str | None = None
