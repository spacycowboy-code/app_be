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

from sqlalchemy import JSON, Boolean, DateTime, Float, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.market_version import RpAiMarketItemVersion
    from app.models.user import RpAiPortal

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
    # How the row got here. Not an enum in the database, and read in only two
    # places — market.py checks ``startswith("gitlab")`` to word a duplicate
    # message, and the review page checks ``!= "upload"`` to explain why it cannot
    # list files — so a new value behaves like ``manual`` in both, which is what a
    # registered pointer should do.
    #   manual                        someone filled in the form
    #   upload                        a package pushed through /api/market/agent-upload
    #   gitlab-tools | gitlab-agents  synced from the two GitLab subgroups
    #   radioai                       the one-off catalogue import, 2026-08-31
    #                                 (tools/import_radioai_agents.py)
    # Kept specific rather than lumped into ``manual`` so a batch someone else
    # produced stays identifiable: it is the only way to find these rows again, to
    # correct them in bulk, or to take them back out.
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
    # Git object sha of this asset's own directory, for items synced from a git
    # tree (see services/gerrit_assets). Only a change detector — deliberately
    # not ``last_activity``, which is a timestamp the card renders.
    content_sha: Mapped[str | None] = mapped_column(String(64))
    is_trending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- externally hosted packages ----------------------------------------
    # 包体不在门户这边，下载走 307 跳到上游。形状与 ``RpAiSkill.external_url`` /
    # ``external_source`` 一字不差 —— 技能侧 2026-09 就是这么接 IVC/RVC 的 Nexus 的，这里刻意
    # 不发明第二套机制。
    #
    # **为什么需要它。** 在这之前市场条目只有两种下载可能：id 是 ``gl-<项目id>`` 的去 GitLab 取
    # 打包，其余一律 409「没有可下载的代码包」。于是上游托管的 agent 根本没法登记成一个能下载的
    # 条目 —— 卡片上有按钮、点下去必然报错。
    #
    # 1024 而不是 512（``repo_url``/``service_url`` 的宽度）：Nexus 的直链带完整仓库路径、
    # 版本号和 runtime 段，实测已经 130 字符出头，而被截断的 URL 在库里是"存住了"、点下去是
    # 404，正是最难查的那种失败。与技能侧同宽。
    external_url: Mapped[str | None] = mapped_column(String(1024))
    # 哪个上游。与 ``RpAiSkill.external_source`` 同一套取值（今天只有 ``nexus-ivc``）。
    external_source: Mapped[str | None] = mapped_column(String(32))
    # 这个资产在上游那边的身份（IVC 的 web 资产是它的 workspace UUID）。
    #
    # **为什么不把它编码进 ``id``。** 同步来的技能和 code 型条目走的是 ``ivc-<对方的 id>``，
    # 那套能用是因为那些行本来就是 ``ivc_sync`` 按对方的 id 建的。web 型不行：生产上那 12 行
    # 是人在界面上手工建的，``id`` 是**显示名的 slug**，和对方的 id 毫无关系 —— 而其中
    # ``ivc-globalknowledge`` 那几行开头的 ``ivc-`` 是名字叫「IVC-GlobalKnowledge」slug 化之后
    # 碰巧带上的，不是前缀。靠拼前缀去匹配，撞上撞不上全是巧合，撞不上就多一张重复卡片。
    #
    # 所以主键只当主键用，外部身份单独存一列，去重是一次索引查询而不是字符串拼接。
    external_ref: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        # 一个上游资产在门户里只能有一行。人工建的行这一列是 NULL，而 MySQL 的唯一索引允许
        # 多个 NULL，所以既有那几十行不受影响。
        UniqueConstraint(
            "external_source", "external_ref", name="uq_market_item_external_ref"
        ),
    )

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

    # Read-only join on the EID string. ``creator`` carries no foreign key (a
    # GitLab-synced item has no creator at all, and an EID that never signed in
    # has no user row), so this is ``viewonly`` and must tolerate finding nothing.
    creator_user: Mapped["RpAiPortal | None"] = relationship(
        primaryjoin="RpAiMarketItem.creator == RpAiPortal.eid",
        foreign_keys="RpAiMarketItem.creator",
        viewonly=True,
        lazy="selectin",
    )

    @property
    def externally_hosted(self) -> bool:
        """包体在上游，下载要跳过去而不是在这边打包。照抄 ``RpAiSkill.externally_hosted``。

        判据是 ``external_url`` 非空，**不是** ``external_source``：来源只是标签，而"能不能下载"
        取决于有没有地址。反过来判会让一个填了来源、忘了地址的条目看起来可下载。
        """
        return bool(self.external_url)

    @property
    def uploader_name(self) -> str | None:
        """The creator's LDAP display name, from the linked user row."""
        user = self.creator_user
        return user.display_name if user else None

    @property
    def creator_label(self) -> str | None:
        """The name to show as this item's creator.

        Mirrors ``RpAiSkill.creator_label`` deliberately, because the two markets
        show the same thing in the same place and two rules would drift. The
        package-upload path writes the uploader's EID into ``author`` while the
        manual-registration form lets someone type a real name, so a card showed
        ``Shawn Xiao`` beside ``EWNGFEI`` — same field, two meanings. Resolving it
        here rather than in the template keeps that decision in one place.

        When ``author`` differs from the creator's EID it is a label a person
        chose (``Anthropic``, ``AI Team``, ``RTE RU SW``) and is shown verbatim;
        otherwise it is just the EID, so the display name is preferred and the EID
        is the fallback — an unresolvable EID still answers "who uploaded this",
        an empty label does not.
        """
        if self.author and self.author != self.creator:
            return self.author
        return self.uploader_name or self.creator
