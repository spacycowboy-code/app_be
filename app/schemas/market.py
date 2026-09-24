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
    #: 这个工具的 OpenAPI spec 地址。填了它，这个工具就能在 Kiro 对话里被**直接调用**
    #: （rp-ai-mcp 的 dispatcher 拿它取契约），使用者不必安装任何东西。
    #:
    #: 可选，绝大多数条目没有。它和 ``service_url`` 是两件事：后者是给人点开的入口，
    #: 这一个必须是机器可读的 spec —— 见 ``models/market.py`` 的同名列。
    openapi_url: str | None = None
    embeddable: bool = False
    category_id: str | None = None
    category: str | None = None
    dept_color: str | None = None
    tags: list[str] = Field(default_factory=list)
    #: 这个资产对应 R&D Guide 上的哪几个工具。可选、可多个。
    #:
    #: **和上面那个 ``tags`` 是两件事。** ``tags`` 是上传者自由输入的关键词;这一个的取值只能
    #: 来自 R&D Guide 现有的工具名(``GET /api/rsw/rdg/tools``),而且只有 Radio SW 的资产能用。
    #: 混进 ``tags`` 的话任何人手打一个同名词就能把自己的资产塞进 R&D Guide 的卡片计数里。
    #:
    #: ``None`` 与 ``[]`` 在编辑接口上**不是一回事**:``None`` 是「这次不改关联」,``[]`` 是
    #: 「清空关联」。建条目时两者等价。
    rdg_nodes: list[str] | None = None
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
    #: OpenAPI spec 地址。**有值即"可以被直接调用"** —— rp-ai-mcp 就是靠这个字段判断
    #: 要不要把这个条目列进"现在就能调的工具"。为空是绝大多数条目的常态。
    openapi_url: str | None = None
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
    # The name to show as the creator. ``author`` holds the uploader's EID on the
    # package-upload path, so a card that rendered it printed a bare EID next to
    # cards showing real names; this is the resolved label (see
    # ``RpAiMarketItem.creator_label``) and is what the card should display.
    creator_label: str | None = None
    uploader_name: str | None = None
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
    #: 上传者给这一版写的说明,可选。空白与「没写」是同一个值(``None``),见
    #: ``agent_upload._clean_note`` —— 一个空字符串在版本历史里会渲染成一个看不出来
    #: 是「写了空的」还是「没写」的空格子。
    notes: str | None = None
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
