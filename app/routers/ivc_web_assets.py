"""IVC 侧 web 型资产的登记端点：``POST /api/integrations/ivc/web-assets``。

**它解决的是什么。** IVC 的资产分三种形态，此前只有两种有入口：

* skill —— ``ivc_sync.py`` 的索引同步（生产 29 行）
* 带包体的 agent —— 同一条同步，``service_type=code``，下载转往 Nexus（生产 2 行）
* **web 型的 agent / tool** —— ``service_type=cloud``，用户点开就跳到 ``serviceUrl``。
  这类此前**只能人工在门户界面上填表**：生产有 12 行这样的条目（``source=manual``、
  ``external_source`` 为空），库里认不出它们是 IVC 的。其中只有 3 行 id 以 ``ivc-`` 开头，
  而那是显示名 slug 化的巧合，不是前缀 —— 剩下 9 行叫 ``hwd-chatbot``、``rvc-ai-agent``、
  ``lab-dashboard`` 之类。

**为什么必须先有这个接口，再谈访问量。** ``rp_ai_market_download`` 与
``rp_ai_usage_event`` 的落点都指向 ``rp_ai_market_item``,父行不存在时
``POST /api/integrations/ivc/usage/events`` 推来的 ``open`` 只能标成 ``skipped``。所以这两个
端点是一对:这个负责"这个资产存在",那个负责"它被用了多少次"。

契约::

    POST /api/integrations/ivc/web-assets
    Authorization: Bearer <IVC_PUSH_TOKEN>

    {"assets": [
      {"externalId":"6301963c-321b-4546-acfc-95406f5c9cae","kind":"agent",
       "name":"IVC-GlobalKnowledge","description":"RAG for global knowledge",
       "serviceUrl":"https://radio-verification-cloud.internal.ericsson.com"
                    "/idea-hub/explore/workspace/6301963c-…?mode=advanced-chat",
       "tags":["IVC","rag"],"version":"1.0.0","author":"ESIYWU",
       "categoryId":"ivc","embeddable":false,
       "createdAt":"2025-11-17T15:37:54+08:00",
       "updatedAt":"2026-09-24T15:49:13+08:00"}
    ]}

``externalId`` / ``kind`` / ``name`` / ``serviceUrl`` 必填，其余可省。``kind`` 是
``agent | tool``（对上门户的 ``item_type``）。

**身份存在 ``external_ref``，不编码进主键。** 这是这个接口最容易被"简化"掉、而简化之后会静默
出错的一点。同步来的技能走 ``ivc-<对方的 id>`` 能用，是因为那些行本来就是 ``ivc_sync`` 按对方
的 id 建的；web 型不行 —— 生产那 12 行是人在界面上手工建的，主键是**显示名的 slug**，而
``ivc-globalknowledge`` 开头的 ``ivc-`` 是名字「IVC-GlobalKnowledge」slug 化之后碰巧带上的，
不是前缀。靠拼前缀匹配，撞上撞不上全是巧合，撞不上就多一张重复卡片。

所以匹配分两级（见 ``_find``）：``external_ref`` 命中就更新；否则从 ``serviceUrl`` 抽
workspace UUID 去比库里那些行的 ``service_url``，命中就**认领**（``action=claimed``）。抽不到
就新建，不猜 —— 名字在这批数据上会直接误判：门户「IVC Auto-Recovery Agent」在 IVC 侧叫
「Recover Workflow」，门户「CIRC(…) RAG」在 IVC 侧叫「CSR」。

**认领只建立身份，不动人工填的内容。** 显示名、描述、分组保持门户现有值：显示名是用户在市场页
上搜索和认卡片的依据，分组决定部门统计把这个资产算给谁（``hwd-chatbot`` 跑在 IVC 平台上但归属
RD2，门户里在 ``radio-design-2``）。``serviceUrl`` 是例外、允许覆盖 —— 对方是自己服务地址的
权威，而库里那个值恰恰可能是错的：生产上有 4 行贴的是 staging 环境的链接甚至是 IVC 的建器页面。

``createdAt`` / ``updatedAt`` 收带时区 ISO 8601 或秒级 epoch，与 ``usage/events`` 的
``occurredAt`` 共用同一个校验器（裸本地时间一律拒）。两者都可选，但 ``createdAt`` 不给会让
月报的「本月新增资产」把一批早就存在的资产算进登记当月，所以缺了会在 ``detail`` 里报出来，
而不是静默按今天算。``createdAt`` 只在首次创建时写入；``updatedAt`` 每次都写进
``last_activity``（卡片上的「最后更新」）。

``embeddable`` 收下但**不应用**，理由见 ``_upsert`` 结尾。

**鉴权复用 ``ivc_push_token``（``require_push_token``），不新增第三个 token。** 这是 2026-09-24
操作者定的：对方的推送器已经拿着那串值，再发一个意味着他们那边多一处配置、我们多一处轮换。

代价写在这里，因为它不体现在任何一行代码里：那串值原本的作用域是「往一张流水表追加行」，
现在它还能**在所有人的市场页上加一张卡片**，并且能把一行人工登记的条目的 ``source`` 改写成
IVC。于是「只想让他们推用量、不想让他们建条目」这个决定没有对应的动作 —— 吊销它会把两件事
一起停掉。真要分开，加回一个 ``ivc_asset_token`` 并把这里换成它自己的闸门即可，端点其余部分
不用动。

**为什么 ``status`` 一律 ``approved``、不走人工审核。** 与 ``ivc_sync`` 一致：审核门是为
**上传的包体**设的（"谁让这个包进来的"要答得出），而这里登记的是一个指向对方服务的链接，
没有包体、也没有在门户里执行任何东西。真正的信任边界是那个 token。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.schemas.common import CamelModel
# 时间校验与两个端点共用,不另写一份:见 ``aware_or_epoch`` 的 docstring。
from app.schemas.external_usage import aware_or_epoch
# 与同步那边共用分组常量,不另抄字面量:抄了就埋一个"哪天改了、这边没跟上"的静默失配。
from app.routers.ivc_sync import IVC_CATEGORY_ID, IVC_SOURCE
# 主键用人工路径那一套 slug 规则,不另写一份 —— 两套 id 风格混在同一张表里,谁都说不清某个
# id 是怎么来的。见 ``_new_portal_id``。
from app.routers.market import _slugify
# 鉴权复用推送那道闸门,不写第二份:两份 hmac 比较迟早漂成一份带 hmac、一份用 ==。
# ``_to_db_naive`` 同理 —— 全仓时间列都是库本地 naive,换算规则只该有一处。
from app.routers.external_usage import _to_db_naive, require_push_token
from app.services.asset_owner import DirectoryDepartments
from app.services.portal_users import ensure_portal_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations/ivc", tags=["ivc-web-assets"])

#: 单批上限。与 ``MAX_EVENTS_PER_BATCH`` 同一个理由,数字小得多是因为资产条目是目录级的:
#: IVC 全部 web 型资产也就几十个,一批 100 已经远超实际需要,而上限的作用是让 413 有个明确
#: 边界,不是卡合理用法。
MAX_ASSETS_PER_BATCH = 100

#: 这两种对上 ``rp_ai_market_item.item_type``。``ai-platform`` 不收:前端把它过滤掉不显示,
#: 收下来会造出一张谁都看不到的卡片。
KIND_AGENT = "agent"
KIND_TOOL = "tool"
ALLOWED_KINDS = frozenset({KIND_AGENT, KIND_TOOL})

#: web 型资产在门户里的 ``service_type``。写死而不是让对方传:这个端点的定义就是"web 型",
#: 允许传 ``code`` 会让它变成一个能悄悄造出可下载条目的通用入口。
SERVICE_TYPE_CLOUD = "cloud"

ACTION_CREATED = "created"
ACTION_UPDATED = "updated"
ACTION_UNCHANGED = "unchanged"
ACTION_SKIPPED = "skipped"
#: 认领了一行门户上已经存在的条目 —— 通常是人在界面上手工建的那批。
#:
#: 单独给一个动作而不是复用 ``updated``，是因为对方在第一次对接时必须能一眼分辨"接上了历史
#: 条目"和"新建了一张卡片"。预期认领却拿到 ``created``，意味着 UUID 没对上，这时候要停下来
#: 查，而不是继续推 —— 继续推会攒出一批需要人工清理的重复卡片。
ACTION_CLAIMED = "claimed"

#: 不能由外部指定的分组。``all`` 是筛选器不是归属；``official`` 表示门户审过，不该由一个
#: token 认证的机器入口自己授予。
CLOSED_CATEGORIES = frozenset({"all", "official"})

#: 从 IVC 的链接里取 workspace UUID，例如
#: ``/idea-hub/explore/workspace/6301963c-321b-4546-acfc-95406f5c9cae?mode=advanced-chat``。
#:
#: **这是认领历史行唯一可用的键。** 那 12 行是人工建的，门户 ``id`` 是显示名的 slug、和对方的
#: id 无关；按名字也对不上 —— 门户叫「IVC Auto-Recovery Agent」而 IVC 侧叫「Recover Workflow」，
#: 门户叫「CIRC(Customer Issue Reproduce and Close-loop) RAG」而 IVC 侧叫「CSR」。
#: 唯一两边都有的值就是当初那个人从 IVC 页面上复制链接时带过来的这个 UUID。
WORKSPACE_UUID = re.compile(
    r"/workspace/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


class WebAssetIn(CamelModel):
    """一个 web 型资产。"""

    external_id: str = Field(min_length=1, max_length=128)
    kind: str
    name: str = Field(min_length=1, max_length=200)
    service_url: str = Field(min_length=1, max_length=512)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    version: str | None = Field(default=None, max_length=32)
    #: 负责人工号。给了才能进部门报告的归属链 —— 见 ``_upsert`` 里 ``ensure_portal_user``
    #: 那一段的理由。
    author: str | None = Field(default=None, max_length=64)
    #: **收下但不应用。** 内嵌是把对方的页面放进门户的同源框架里，那是一个钓鱼面（用户看地址栏
    #: 还是门户），该由人看过那个页面之后决定，不该由推送方设置。留着这个字段只是为了让对方现有
    #: 的载荷能解析通过，语义写在这里而不是静默忽略。见 ``_upsert`` 结尾那段。
    embeddable: bool = False
    #: 归到哪个分组。不给就落 IVC 自己那个。
    #:
    #: 需要这个字段是因为「谁托管」和「谁的资产」是两件事：``hwd-chatbot`` 跑在 IVC 平台上，
    #: 但它是 RD2 的，门户里归在 ``radio-design-2``。写死 ``ivc`` 会把 RD2 的资产算到 IVC 头上，
    #: 而部门页和月报都读这一列。只在**创建**时生效 —— 已有行的分组可能被人在界面上调过。
    category_id: str | None = None
    #: **这个资产在对方那边的创建时间。** 可选,但不给会让数字失真,所以缺了要报出来。
    #:
    #: 月报的「本月新增资产」按 ``RpAiMarketItem.created_at`` 在窗口内计数
    #: (``routers/usage.py`` 的 ``items_added``)。批量登记一批早就存在的资产时,如果让
    #: ``created_at`` 走库的 server default,那一批会整体算进登记当月的新增 —— 而它们可能
    #: 一年前就有了。同一个理由让 ``usage/events`` 的 ``occurredAt`` 是必填项。
    #:
    #: 只在**首次创建**时写入,更新时不动:它是资产的出生时间,不是这次请求的时间。
    created_at: datetime | int | None = None
    #: 对方那边这个资产最后一次变更的时间。写进 ``last_activity`` —— 卡片上渲染的
    #: 「最后更新」就是这一列,同步来的条目写的是 GitLab 的 ``last_activity_at``,所以这是
    #: 既有口径而不是新造一个。不给就留空,卡片不显示那一行。
    updated_at: datetime | int | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def _aware_or_epoch(cls, value: datetime | int | None) -> datetime | None:
        """与 ``usage/events`` 的 ``occurredAt`` 共用一套校验，裸本地时间一律拒。

        复用而不是另写一份：两份迟早漂成一份拒裸时间、一份猜一个偏移，而那种不一致只体现在
        数字上、不报错。
        """
        if value is None:
            return None
        return aware_or_epoch(value)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in ALLOWED_KINDS:
            allowed = " | ".join(sorted(ALLOWED_KINDS))
            raise ValueError(f"unknown kind: {value!r}（合法取值：{allowed}）")
        return value

    @field_validator("external_id")
    @classmethod
    def _plain_id(cls, value: str) -> str:
        """去掉两端空白，拒空白串与控制字符。

        它不再被拼成主键（主键是显示名的 slug，见 ``_new_portal_id``），所以路径字符不必拦。
        但它是唯一索引的一半、而且会被原样回显进 ``outcomes``，所以不能带控制字符：一个带
        ``\\n`` 的 id 在日志里会把一行断成两行，排查时看起来像两条不同的记录。
        """
        ident = value.strip()
        if not ident:
            raise ValueError("externalId 不能为空")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in ident):
            raise ValueError("externalId 不能含控制字符")
        return ident

    @field_validator("service_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        """只收 http/https。

        这个值会被渲染成卡片上的链接，而卡片的打开动作是
        ``window.open(url, '_blank', 'noopener')``（``tool-agent-market.component.ts``）。
        ``javascript:`` 开头的地址在那里是货真价实的 XSS 向量，而这个端点是 token 认证的机器
        入口，中间没有人会看一眼。

        （前端今天**没有**按 ``embeddable`` 渲染 iframe —— 全仓唯一读那个字段的地方是编辑弹窗
        回填勾选框。所以这道校验的理由是 ``window.open``，不是 iframe 的 ``src``。）
        """
        url = value.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("serviceUrl 必须是 http:// 或 https:// 的绝对地址")
        return url

    @field_validator("author")
    @classmethod
    def _upper_eid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        eid = value.strip().upper()
        return eid or None


class WebAssetBatchIn(CamelModel):
    """一批。批量而不是一条一请求，理由同 ``ExternalUsageBatchIn``。"""

    assets: list[WebAssetIn] = Field(min_length=1)


class WebAssetOutcome(CamelModel):
    """逐条结果。字段名照 ``ivc_sync.IvcSyncOutcome``，对方已经在解析那个形状了。"""

    external_id: str
    id: str
    action: str
    detail: str = ""


class WebAssetResult(CamelModel):
    """整批结果。计数与逐条都给，理由同 ``ExternalUsageResult``。"""

    received: int
    created: int = 0
    claimed: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    outcomes: list[WebAssetOutcome] = Field(default_factory=list)


def _new_portal_id(db: Session, name: str) -> str:
    """给一个新条目挑主键。与人工登记路径同一套规则（``market.py`` 的建条目那一段）。

    **为什么不是 ``ivc-<externalId>``。** IVC 的 ``externalId`` 是 workspace UUID，拼出来是
    ``ivc-6301963c-321b-4546-acfc-95406f5c9cae`` —— 一个人读不懂的主键，而它会直接出现在
    市场的前端路由里。而且同一张表里一半 id 是显示名的 slug、一半是 UUID，日后没人说得清某个
    id 是怎么来的。

    身份不靠主键承载（那是 ``external_ref`` 的事），所以主键可以只为可读性服务。
    """
    base = _slugify(name) or "ivc-asset"
    item_id = base
    n = 2
    while db.get(RpAiMarketItem, item_id):
        item_id = f"{base}-{n}"
        n += 1
    return item_id


def _find(db: Session, asset: WebAssetIn) -> tuple[RpAiMarketItem | None, str | None]:
    """按身份找门户里那一行。返回 ``(行或 None, 命中方式)``。

    两级，从确定到推测：

    1. ``external_ref`` 命中 —— 这行上次就是这个上游资产，无歧义。
    2. 从 ``serviceUrl`` 抽出的 workspace UUID 命中某行的 ``service_url`` —— 那是一行人工建的
       历史条目，这次把它**认领**过来。

    第 2 级敢自动做，是因为 workspace UUID 由 IVC 自己生成，两个不同资产不可能同一个值。这比
    "名字相似"或"URL 前缀相同"强得多 —— 后两者在这批数据上会直接误判：门户的
    「IVC Auto-Recovery Agent」在 IVC 侧叫「Recover Workflow」。

    抽不到 UUID 就不猜，直接走新建。猜错的代价是把两个不同资产合成一行，不可逆。
    """
    hit = db.scalars(
        select(RpAiMarketItem).where(
            RpAiMarketItem.external_source == IVC_SOURCE,
            RpAiMarketItem.external_ref == asset.external_id,
        )
    ).first()
    if hit is not None:
        return hit, "ref"

    found = WORKSPACE_UUID.search(asset.service_url)
    if found is None:
        return None, None
    uuid = found.group(1).lower()
    # 只在 cloud 型里找：code 型条目的 ``service_url`` 是空的，而 ``ai-platform`` 那几行前端
    # 根本不显示，认领它们只会让一张看不见的卡片换个来源。
    for row in db.scalars(
        select(RpAiMarketItem).where(
            RpAiMarketItem.service_type == SERVICE_TYPE_CLOUD,
            RpAiMarketItem.service_url.is_not(None),
        )
    ):
        other = WORKSPACE_UUID.search(row.service_url or "")
        if other and other.group(1).lower() == uuid:
            return row, "uuid"
    return None, None


def _upsert(
    db: Session,
    asset: WebAssetIn,
    directory: DirectoryDepartments,
) -> tuple[str, str, str]:
    """建、认领或更新一行。返回 ``(portal_id, action, detail)``。"""
    category_id = asset.category_id or IVC_CATEGORY_ID
    if category_id in CLOSED_CATEGORIES or db.get(RpAiCategory, category_id) is None:
        return (
            asset.external_id,
            ACTION_SKIPPED,
            f"分组「{category_id}」不能登记到，或不存在",
        )

    item, how = _find(db, asset)
    changes: list[str] = []

    if item is None:
        portal_id = _new_portal_id(db, asset.name)
        item = RpAiMarketItem(
            id=portal_id,
            name=asset.name,
            item_type=asset.kind,
            service_type=SERVICE_TYPE_CLOUD,
            category_id=category_id,
            status="approved",
        )
        # 只在创建时写。给了就用对方那边的出生时间,否则月报的「本月新增」会把一批早就存在的
        # 资产算进登记当月。没给时退回库的 server default(= 登记时刻),但**必须报出来** ——
        # 一个静默按今天算的创建时间,事后没人能分辨它是真的今天建的还是补登记的。
        if asset.created_at is not None:
            item.created_at = _to_db_naive(asset.created_at)
        else:
            changes.append("未给 createdAt，创建时间按登记时刻算")
        db.add(item)
        action = ACTION_CREATED
    elif item.item_type != asset.kind:
        # 类型对不上就整条停下，不静默采纳任意一边:采纳门户的,对方按自己那个类型统计、我们按
        # 另一个,数字对不上而两边各自自洽;采纳对方的,一次推送就能让条目在市场页上换一个页签,
        # 而没有人做过那个决定。
        return (
            item.id,
            ACTION_SKIPPED,
            f"类型对不上：你给的是 {asset.kind}，门户里 {item.id} 是 {item.item_type}。"
            f"改一处再重推",
        )
    elif how == "uuid":
        # **认领：只建立身份，不动人工填的内容。**
        #
        # 这批历史行的显示名是人写的，而且和 IVC 侧对不上 —— 门户「IVC Auto-Recovery Agent」
        # 对 IVC 的「Recover Workflow」、门户「CIRC(…) RAG」对 IVC 的「CSR」。覆盖了，用户在
        # 市场页上再也找不到他们认识的那张卡片，而描述是 Text、原文没有备份。
        #
        # ``category_id`` 同理不动：``hwd-chatbot`` 归在 ``radio-design-2`` —— 它托管在 IVC
        # 平台上但归属 RD2，门户已经填对了。按登记方给的分组改会把 RD2 的资产算到 IVC 头上。
        action = ACTION_CLAIMED
        changes.append(f"认领门户已有条目 {item.id}（原 source={item.source}）")
        changes.append("显示名、描述、分组保留门户现有值")
    else:
        if item.name != asset.name:
            changes.append(f"名字保持 {item.name}（登记接口不改显示名）")
        if item.service_url != asset.service_url:
            changes.append("服务地址")
        if item.version != asset.version:
            changes.append(f"版本 {item.version} -> {asset.version}")
        if item.description != asset.description:
            changes.append("描述")
        action = ACTION_UPDATED if changes else ACTION_UNCHANGED

    # ── 身份：三条路径都写 ────────────────────────────────────────────────────
    item.external_ref = asset.external_id
    item.external_source = IVC_SOURCE
    item.source = IVC_SOURCE
    item.service_type = SERVICE_TYPE_CLOUD
    # 服务地址**是认领的例外，允许覆盖**。对方是自己服务地址的权威，而库里那个值恰恰可能是错的:
    # 生产上有 4 行贴的是 staging 环境的 workspace 链接、甚至是 IVC 的建器页面,用户点进去到
    # 的不是那个 agent。
    item.service_url = asset.service_url
    # 卡片上「最后更新」读的就是这一列(字符串,同步来的条目写 GitLab 的 last_activity_at),
    # 所以沿用它而不是新造一列。不给就留空,卡片不显示那一行 —— 比显示一个登记时刻好:
    # 后者会让人以为这个资产今天改过。
    if asset.updated_at is not None:
        item.last_activity = _to_db_naive(asset.updated_at).isoformat()
    # 内容在对方那边、而且是一个在线服务:这两个必须留空。``repo_url`` 填了会让下载路径去
    # GitLab 找一个不存在的项目;``external_url`` 是 Nexus 直链那一套(带包体的资产用),
    # 填了会让卡片同时给出"打开"和"下载"两个动作,而后者下不到东西。
    item.repo_url = None
    item.external_url = None

    # ── 元数据：认领那一次不动，其余情况同步 ──────────────────────────────────
    #
    # ``name`` 和 ``category_id`` 只在创建时写，之后**永不覆盖**:显示名是用户在市场页上搜索和
    # 认卡片的依据,分组决定部门统计把它算给谁 —— 两者都可能被人在界面上调过,而那是业务决定,
    # 不该被一次自动同步推翻。代价是 IVC 改了名字不会同步过来,需要人改一次。
    #
    # ``embeddable`` 一律不写。内嵌是把对方的页面放进门户的同源框架里（钓鱼面),该由人看过那个
    # 页面之后决定。今天前端还没消费这个字段,但那个功能一做起来,批量推进来的 true 会一夜之间
    # 全部生效,而没有人为此做过决定。
    if action != ACTION_CLAIMED:
        item.description = asset.description
        item.tags = asset.tags
        item.version = asset.version
        if asset.author:
            item.author = asset.author
            item.creator = asset.author

    # 报告的归属链被 ``rp_ai_portal`` 那张表框死（``usage_report`` 的 ``build_people``），
    # 用户行不存在时这个资产会落进「无法归属」，而且不报错。所以补一行带部门的用户行。
    # 认领的行也补 —— 归属链和显示名无关，缺了一样会漏统计。
    if asset.author:
        ensure_portal_user(
            db,
            asset.author,
            department=directory.department_of(asset.author),
            origin="import",
            lookup_directory=False,
        )

    return item.id, action, "；".join(changes)


@router.post(
    "/web-assets",
    response_model=WebAssetResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="登记 IVC 侧的 web 型 agent / tool（service_type=cloud）",
)
def register_web_assets(
    payload: WebAssetBatchIn = Body(...),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> WebAssetResult:
    """收一批 web 型资产。见模块 docstring 里的契约。

    整批 202 并逐条给结果，理由同 ``external_usage.push_usage_events``：一条落不了地的
    条目不该挡住同批其余的，而只给状态码会让对方以为全收下了。
    """
    require_push_token(authorization)

    if len(payload.assets) > MAX_ASSETS_PER_BATCH:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"单批最多 {MAX_ASSETS_PER_BATCH} 条，这批 {len(payload.assets)} 条",
        )

    result = WebAssetResult(received=len(payload.assets))

    # 一批共用一条 LDAP 连接，照 ``ivc_sync`` 的先例：一批可能有十几个负责人，每人各开一次
    # bind 既慢，又会在目录侧看起来像在扫号。
    directory = DirectoryDepartments()
    try:
        for asset in payload.assets:
            # 每条一个 SAVEPOINT：一条坏数据不该把同批其余的一起回滚，而"整批失败"在对方
            # 那边看起来就是"门户挂了"。
            try:
                with db.begin_nested():
                    portal_id, action, detail = _upsert(db, asset, directory)
            except Exception:  # noqa: BLE001
                logger.exception("登记资产 %s 失败", asset.external_id)
                # 出错时用对方的 id 回复，而不是编一个门户 id：这一条到底落在哪一行正是没算出来
                # 的那个东西，编一个会让对方按一个不存在的 id 去查。
                portal_id = asset.external_id
                action, detail = ACTION_SKIPPED, "门户内部错误，见服务端日志"

            if action == ACTION_CREATED:
                result.created += 1
            elif action == ACTION_CLAIMED:
                result.claimed += 1
            elif action == ACTION_UPDATED:
                result.updated += 1
            elif action == ACTION_UNCHANGED:
                result.unchanged += 1
            else:
                result.skipped += 1
            result.outcomes.append(
                WebAssetOutcome(
                    external_id=asset.external_id,
                    id=portal_id,
                    action=action,
                    detail=detail,
                )
            )
    finally:
        directory.close()

    db.commit()
    logger.info(
        "IVC web assets registered: created=%d updated=%d unchanged=%d skipped=%d",
        result.created, result.updated, result.unchanged, result.skipped,
    )
    return result
