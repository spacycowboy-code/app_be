"""Pydantic schemas for skills (rp_ai_skill) and their child records."""
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.card_group import CardGroupRead
from app.schemas.common import CamelModel

SkillStatus = Literal["pending", "approved", "rejected"]


# --- child schemas ---------------------------------------------------------
class SkillCaseSchema(CamelModel):
    title: str
    image: str | None = None


class SubSkillSchema(CamelModel):
    name: str
    # ORM attribute is ``description``; frontend JSON key is ``desc``.
    description: str | None = Field(default=None, alias="desc")
    badge: str | None = None
    icon_color: str | None = None
    icon_bg: str | None = None


class InputParamSchema(CamelModel):
    name: str
    type: str | None = None
    required: bool = False
    description: str | None = Field(default=None, alias="desc")


# --- skill schemas ---------------------------------------------------------
class SkillBase(CamelModel):
    """What the author of a skill provides.

    Deliberately does not carry the fields the server derives — rating, downloads,
    review counts, trending, lifecycle status, version, resolved names. They used
    to live here, and because this model is also the input model, a client could
    set its own skill to ``approved`` or write its own rating and download count.
    A field belongs on the input surface only if the person submitting the skill
    is the one who decides it.
    """

    name: str
    description: str | None = None
    category: str | None = None
    category_id: str | None = None
    image: str | None = None
    is_free: bool = True
    tags: list[str] = Field(default_factory=list)
    capability_full: str | None = None
    example_prompt: str | None = None
    example_output: str | None = None
    instructions: str | None = None

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v: object) -> object:
        return v if v is not None else []


class SkillDerived(CamelModel):
    """What the server decides. Read-only from the API's point of view.

    ``status`` is written by exactly three places: the upload gate (from the audit
    outcome), the official import (auto-approved), and the reconciler (from the
    review MR's state). None of them is the client.
    """

    rating: float = 0.0
    downloads: int = 0
    is_trending: bool = False
    version: str | None = None
    author: str | None = None
    uploader_eid: str | None = None
    # LDAP display name of the uploader (read-only, resolved from the user row).
    uploader_name: str | None = None
    # Name to show as the creator (person's display name, or imported label).
    creator_label: str | None = None
    rating_dist: list[int] | None = None
    review_count: int = 0
    status: SkillStatus = "pending"
    # Why a skill is not public yet: pass | suspect | danger. Without it an admin
    # sees "pending" and cannot tell whether it is waiting on a review or on
    # something else.
    audit_state: str | None = None


class SkillCreate(SkillBase):
    # Client may supply a slug id; if omitted the server generates one.
    id: str | None = None
    cases: list[SkillCaseSchema] = Field(default_factory=list)
    sub_skills: list[SubSkillSchema] = Field(default_factory=list)
    input_params: list[InputParamSchema] = Field(default_factory=list)


class SkillUpdate(CamelModel):
    """The editable surface. Same rule as SkillBase: only what the author decides.

    Extra keys are ignored rather than rejected, so a client that still posts the
    whole skill object (rating, downloads, status) keeps working — those values
    are simply not writable any more.
    """

    name: str | None = None
    description: str | None = None
    category: str | None = None
    category_id: str | None = None
    image: str | None = None
    is_free: bool | None = None
    tags: list[str] | None = None
    capability_full: str | None = None
    example_prompt: str | None = None
    example_output: str | None = None
    instructions: str | None = None
    cases: list[SkillCaseSchema] | None = None
    sub_skills: list[SubSkillSchema] | None = None
    input_params: list[InputParamSchema] | None = None
    # EID of the user performing the edit (recorded in the version history).
    editor_eid: str | None = None
    # Full SKILL.md content — when provided, it is written verbatim as the
    # skill's definition file (name/description are synced from its frontmatter).
    skill_md: str | None = None


class PendingUpdateRead(CamelModel):
    """作者提交了、正在人工复核、还没发布的那次更新。

    它存在的时候,这个技能其余所有字段描述的都是**已发布**的那一版。前端要靠它回答作者的那个问题:
    "我明明改了,怎么页面上还是旧的" —— 答案是改动在复核里,而旧版本继续服务着使用者。
    """

    version: str | None = None
    mr_url: str | None = None
    audit_ref: str
    editor_eid: str | None = None
    # pending = 还在复核里;rejected = 复核被否了。两个答案不能长得一样:前者要等,后者要改。
    outcome: str = "pending"
    # 暂存的名称/描述,让前端不用再取一次就能显示"待发布的是什么"。
    name: str | None = None
    description: str | None = None
    created_at: datetime | None = None


class SkillImageState(CamelModel):
    """封面图端点的返回形状 —— 刻意只有这三个字段。

    不返回完整的 ``SkillRead``:那些写端点返回的是一个"裸"技能行,``favoriteCount`` 是 0、
    ``group`` 和 ``pendingUpdate`` 是空(它们由 get_skill 里的辅助函数另外附上)。前端若拿它
    整体替换本地对象,收藏数就会在界面上被冲成 0 直到下次刷新。窄返回让这件事不可能发生:
    调用方只能拿到"图现在是什么",也只需要这个。
    """

    id: str
    #: 部门默认图的静态资源路径,始终有值。
    image: str | None = None
    #: 为真时前端应该指向 GET /api/skills/{id}/image;为假时用上面那个路径。
    has_custom_image: bool = False


class ExternalDownloadRead(CamelModel):
    """外部托管技能的「去上游取」的返回形状 —— 刻意只有这三个字段。

    ``url`` 是这一行**当前**的上游地址。界面上那个链接的 ``href`` 用的是读模型里的
    ``externalUrl``(真链接必须在点击之前就有地址,不能等一次请求回来),所以这里返回它不是为了
    给浏览器用,而是为了让这个端点对不渲染页面的调用方(脚本、以后的 CLI)自成一体:记一次账、
    同时知道该去哪儿,不必再取一遍整个技能。
    """

    id: str
    url: str | None = None
    #: 哪个上游(如 ``nexus-ivc``),让界面能说出名字。
    source: str | None = None


class SkillRead(SkillBase, SkillDerived):
    id: str
    # 作者传过自己的封面图吗。为真时前端应该指向 GET /api/skills/{id}/image;为假时用上面
    # ``image`` 那个部门默认图的静态路径。不把字节放进 JSON:那会让每次列表/详情都多传几百 KB。
    has_custom_image: bool = False
    # 内容在别人那儿(第一个来源是 IVC 的 Nexus)。前端要靠这几个字段把「下载」换成一个指向上游
    # 的链接:门户手里没有字节,而一个长得和普通下载一样的按钮只会让人拿到一个解释不了的报错 ——
    # XHR 跟不了跨域跳转,浏览器连状态码都不给。``external_url`` 一并给出,让链接指向的主机在界面
    # 上看得见:下载得先能访问那台机器,这件事该让人自己看出来,而不是点了才知道。
    externally_hosted: bool = False
    external_url: str | None = None
    #: 哪个上游同步来的(如 ``nexus-ivc``)。让界面能说出名字,而不是笼统的"外部"。
    external_source: str | None = None
    cases: list[SkillCaseSchema] = Field(default_factory=list)
    sub_skills: list[SubSkillSchema] = Field(default_factory=list)
    input_params: list[InputParamSchema] = Field(default_factory=list)
    storage_path: str | None = None
    repo_commit: str | None = None
    favorite_count: int = 0
    created_at: datetime
    updated_at: datetime
    # See ``SkillSummary.content_updated_at``: when the content last changed,
    # rather than when the row was last written.
    content_updated_at: datetime | None = None
    # Git sync status of the last write. ``synced`` is None when remote push is
    # disabled or not applicable; False means the change was saved locally but
    # the push to the GitLab remote failed. ``sync_warning`` carries a
    # human-readable message the frontend can surface when synced is False.
    synced: bool | None = None
    sync_warning: str | None = None
    # 压在已发布版本之外、正在人工复核的那次更新(没有就是 None)。上面所有字段描述的都是已发布的
    # 那一版,这个字段是唯一说"还有一份在等复核"的地方。
    pending_update: PendingUpdateRead | None = None
    # Where the uploader sits: 地址 · 大部门 · 小部门. None when the person cannot
    # be placed (no department recorded, or a department outside Radio Products)
    # or when there is no real uploader at all — the official imports.
    group: CardGroupRead | None = None


class SkillSummary(CamelModel):
    """Lightweight shape for list/market views."""

    id: str
    name: str
    description: str | None = None
    category: str | None = None
    category_id: str | None = None
    image: str | None = None
    # 同 SkillRead.has_custom_image —— 市场卡片就是靠它决定图片指哪。
    has_custom_image: bool = False
    # 同 SkillRead —— 市场卡片上的下载按钮也要按它分岔,否则从卡片点下载照样是那个
    # 解释不了的网络错误。
    externally_hosted: bool = False
    external_url: str | None = None
    external_source: str | None = None
    rating: float = 0.0
    downloads: int = 0
    is_trending: bool = False
    is_free: bool = True
    tags: list[str] = Field(default_factory=list)
    author: str | None = None
    uploader_eid: str | None = None
    uploader_name: str | None = None
    creator_label: str | None = None
    favorite_count: int = 0
    status: SkillStatus = "pending"
    # Why it is not public yet, for whoever has to act on the queue.
    audit_state: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # When this skill's *content* last changed, as opposed to when its row was
    # last written. Taken from the newest ``rp_ai_skill_version`` row (one per
    # upload); for skills with no version history — the official imports — it
    # falls back to ``updated_at``. The Home feed uses this to decide what counts
    # as an "Update", so that bookkeeping writes to the row (download counters,
    # ratings, repo bookkeeping) cannot announce a skill as updated.
    content_updated_at: datetime | None = None
    # Where the uploader sits: 地址 · 大部门 · 小部门. See ``SkillRead.group``.
    group: CardGroupRead | None = None
