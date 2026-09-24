"""IVC 侧 web 型资产的登记端点：``POST /api/integrations/ivc/web-assets``。

**它解决的是什么。** IVC 的资产分三种形态，此前只有两种有入口：

* skill —— ``ivc_sync.py`` 的索引同步（生产 29 行）
* 带包体的 agent —— 同一条同步，``service_type=code``，下载转往 Nexus（生产 2 行）
* **web 型的 agent / tool** —— ``service_type=cloud``，用户点开就跳到 ``serviceUrl``。
  这类此前**只能人工在门户界面上填表**：生产那 3 行 ``ivc-*`` 的 ``source`` 是 ``manual``、
  ``external_source`` 为空，也就是说库里认不出它们是 IVC 的。

**为什么必须先有这个接口，再谈访问量。** ``rp_ai_market_download`` 与
``rp_ai_usage_event`` 的落点都指向 ``rp_ai_market_item``,父行不存在时
``POST /api/integrations/ivc/usage/events`` 推来的 ``open`` 只能标成 ``skipped``。所以这两个
端点是一对:这个负责"这个资产存在",那个负责"它被用了多少次"。

契约::

    POST /api/integrations/ivc/web-assets
    Authorization: Bearer <IVC_ASSET_TOKEN>

    {"assets": [
      {"externalId":"replir-allocation","kind":"agent",
       "name":"ReplIR Allocation","description":"...",
       "serviceUrl":"https://ivc.internal.ericsson.com/replir",
       "tags":["allocation"],"version":"1.2.0","author":"EWENLIU",
       "embeddable":false,
       "createdAt":"2025-11-03T09:12:00+08:00",
       "updatedAt":"2026-09-20T16:40:00+08:00"}
    ]}

``externalId`` / ``kind`` / ``name`` / ``serviceUrl`` 必填，其余可省。``kind`` 是
``agent | tool``（对上门户的 ``item_type``）。门户侧 id 是 ``ivc-<externalId>``,与
``ivc_sync`` 用同一个前缀常量 —— 推送方不该知道这个前缀,``usage/events`` 里的 ``assetId``
同样只给 ``externalId``。

``createdAt`` / ``updatedAt`` 收带时区 ISO 8601 或秒级 epoch，与 ``usage/events`` 的
``occurredAt`` 共用同一个校验器（裸本地时间一律拒）。两者都可选，但 ``createdAt`` 不给会让
月报的「本月新增资产」把一批早就存在的资产算进登记当月，所以缺了会在 ``detail`` 里报出来，
而不是静默按今天算。``createdAt`` 只在首次创建时写入；``updatedAt`` 每次都写进
``last_activity``（卡片上的「最后更新」）。

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
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.schemas.common import CamelModel
# 时间校验与两个端点共用,不另写一份:见 ``aware_or_epoch`` 的 docstring。
from app.schemas.external_usage import aware_or_epoch
# 与同步那边共用前缀和分组常量,不另抄字面量:抄了就埋一个"哪天改了、这边没跟上"的静默失配。
from app.routers.ivc_sync import ID_PREFIX, IVC_CATEGORY_ID, IVC_SOURCE
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


class WebAssetIn(CamelModel):
    """一个 web 型资产。"""

    external_id: str = Field(min_length=1, max_length=100)
    kind: str
    name: str = Field(min_length=1, max_length=200)
    service_url: str = Field(min_length=1, max_length=512)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    version: str | None = Field(default=None, max_length=32)
    #: 负责人工号。给了才能进部门报告的归属链 —— 见 ``_upsert`` 里 ``ensure_portal_user``
    #: 那一段的理由。
    author: str | None = Field(default=None, max_length=64)
    #: 允不允许在门户里用 iframe 内嵌预览。默认 false:内嵌是把对方的页面放进门户的同源框架里,
    #: 该由人看过那个页面之后决定,不该由推送方默认打开。
    embeddable: bool = False
    #: 归到哪个分组。默认 IVC 自己那个,与 ``ivc_sync`` 一致。
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
        """id 只收一段朴素标识符。

        它会被拼成门户侧主键 ``ivc-<externalId>``,而那个 id 直接出现在市场的前端路由里。
        放行斜杠或空白会造出打不开的详情页链接,而症状是"卡片在、点进去 404",离原因很远。
        """
        ident = value.strip()
        if not ident or any(c in ident for c in "/\\ ?#%"):
            raise ValueError(
                "externalId 只能是一段朴素标识符，不能含空白或 / \\ ? # %"
            )
        return ident

    @field_validator("service_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        """只收 http/https。

        这个值会被渲染成卡片上的链接,``embeddable`` 为真时还会进 iframe 的 ``src``。
        放行 ``javascript:`` 之类的 scheme 等于把一个存储型 XSS 入口开在市场页上,而这个
        端点是 token 认证的机器入口,没有人会在中间看一眼。
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
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    outcomes: list[WebAssetOutcome] = Field(default_factory=list)


def _upsert(
    db: Session,
    asset: WebAssetIn,
    directory: DirectoryDepartments,
) -> tuple[str, str, str]:
    """建或更新一行。返回 ``(portal_id, action, detail)``。"""
    portal_id = f"{ID_PREFIX}{asset.external_id}"
    item = db.get(RpAiMarketItem, portal_id)
    category_id = asset.category_id or IVC_CATEGORY_ID

    if db.get(RpAiCategory, category_id) is None:
        return portal_id, ACTION_SKIPPED, f"分组「{category_id}」不存在，先建分组"

    if item is None:
        item = RpAiMarketItem(
            id=portal_id,
            name=asset.name,
            item_type=asset.kind,
            service_type=SERVICE_TYPE_CLOUD,
            source=IVC_SOURCE,
            category_id=category_id,
            status="approved",
        )
        # 只在创建时写。给了就用对方那边的出生时间,否则月报的「本月新增」会把一批早就存在的
        # 资产算进登记当月。没给时退回库的 server default(= 登记时刻),但**必须报出来** ——
        # 一个静默按今天算的创建时间,事后没人能分辨它是真的今天建的还是补登记的。
        if asset.created_at is not None:
            item.created_at = _to_db_naive(asset.created_at)
        db.add(item)
        action, changes = ACTION_CREATED, []
        if asset.created_at is None:
            changes.append("未给 createdAt，创建时间按登记时刻算")
    else:
        changes: list[str] = []
        if item.name != asset.name:
            changes.append(f"名字 {item.name} -> {asset.name}")
        if item.item_type != asset.kind:
            changes.append(f"类型 {item.item_type} -> {asset.kind}")
        if item.service_type != SERVICE_TYPE_CLOUD:
            changes.append(f"形态 {item.service_type} -> {SERVICE_TYPE_CLOUD}")
        if item.service_url != asset.service_url:
            changes.append("服务地址")
        if item.version != asset.version:
            changes.append(f"版本 {item.version} -> {asset.version}")
        if item.description != asset.description:
            changes.append("描述")
        if item.category_id != category_id:
            changes.append(f"分组 {item.category_id} -> {category_id}")
        # 生产里有 3 行是人在界面上手工建的（``source=manual``、``external_source`` 为空）。
        # 对方登记同一个 id 时这两列会被改写成 IVC —— 那是我们想要的（这样才认得出它们是谁的、
        # 才能批量纠正），但**必须显式报出来**：悄悄改写一行人建的数据，事后没人解释得清。
        if item.source != IVC_SOURCE:
            changes.append(f"来源 {item.source} -> {IVC_SOURCE}（原为人工登记）")
        action = ACTION_UPDATED if changes else ACTION_UNCHANGED

    item.name = asset.name
    item.item_type = asset.kind
    item.service_type = SERVICE_TYPE_CLOUD
    item.description = asset.description
    item.service_url = asset.service_url
    item.tags = asset.tags
    item.version = asset.version
    item.embeddable = asset.embeddable
    item.category_id = category_id
    item.source = IVC_SOURCE
    item.external_source = IVC_SOURCE
    # 卡片上「最后更新」读的就是这一列(字符串,同步来的条目写 GitLab 的 last_activity_at),
    # 所以沿用它而不是新造一列。不给就留空,卡片不显示那一行 —— 比显示一个登记时刻好:
    # 后者会让人以为这个资产今天改过。
    if asset.updated_at is not None:
        item.last_activity = _to_db_naive(asset.updated_at).isoformat()
    # **``created_at`` 刻意不在这里回写。** 它是资产的出生时间,只在上面那个创建分支里写一次。
    # 每次登记都刷新它的话,对方每跑一次同步就会把整批资产重新算进「本月新增」。
    # 内容在对方那边、而且是一个在线服务:这两个必须留空。``repo_url`` 填了会让下载路径去
    # GitLab 找一个不存在的项目;``external_url`` 是 Nexus 直链那一套(带包体的资产用),
    # 填了会让卡片同时给出"打开"和"下载"两个动作,而后者下不到东西。
    item.repo_url = None
    item.external_url = None

    if asset.author:
        item.author = asset.author
        item.creator = asset.author
        # 报告的归属链被 ``rp_ai_portal`` 那张表框死（``usage_report`` 的 ``build_people``），
        # 用户行不存在时这个资产会落进「无法归属」，而且不报错。所以补一行带部门的用户行。
        ensure_portal_user(
            db,
            asset.author,
            department=directory.department_of(asset.author),
            origin="import",
            lookup_directory=False,
        )

    return portal_id, action, "；".join(changes)


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
                portal_id = f"{ID_PREFIX}{asset.external_id}"
                action, detail = ACTION_SKIPPED, "门户内部错误，见服务端日志"

            if action == ACTION_CREATED:
                result.created += 1
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
