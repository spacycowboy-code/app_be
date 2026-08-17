"""Market item model: rp_ai_market_item.

Backs the Agent市场 (Tool & MCP Server & Agent). An item is either:
- a Code service (points at a git repo), or
- a Cloud service (points at a running web service URL),
and is typed as a Tool or an Agent. Items come from GitLab subgroup
syncs (source = gitlab-tools / gitlab-agents) or manual upload (manual).
"""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin

ITEM_TYPES = ("tool", "agent", "ai-platform")
SERVICE_TYPES = ("code", "cloud")
SOURCES = ("manual", "gitlab-tools", "gitlab-agents")


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

    public_download: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    """是否允许**免登录**下载该项目的代码包。

    存在的理由:有些工具要让 agent 在使用者机器上自动装好,而 agent 手里没有门户
    登录态。让它去问使用者的域账号密码是不可接受的(那是公司域口令,且门户还没有
    TLS),所以只剩「这个接口对该项目免鉴权」这一条路。

    **默认 False,且必须逐项目手动打开。** 做成 opt-in 而不是 opt-out:同步进来的
    新项目自动受保护,不会因为谁忘了配置就把内网仓库的源码裸露出去。打开一项的
    含义是「任何能访问门户的人都能拿到这个仓库的全部代码」,这个判断只能由人逐个做。

    打开之后有两个后果,决定前要知道:该项目的下载不再有可信的下载者身份(``eid``
    只能由调用方自报),门户也还没有 TLS,代码在内网中明文传输。
    """
    # EID of the user who manually created this item (None for GitLab-synced).
    creator: Mapped[str | None] = mapped_column(String(64))
