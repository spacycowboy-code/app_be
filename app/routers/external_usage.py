"""RVC 用量推送端点：``POST /api/integrations/ivc/usage/events``。

RVC 那边的资产（skill / agent）托管在 Nexus 上，人是在他们那边下载和点赞的，门户看不见。
这个端点让他们把用量推过来，于是市场卡片上的下载人数、部门页和月报能把 RVC 的量算进去。

契约（2026-09-02 与 RVC 谈定，``version`` 最后商量掉了）::

    POST /api/integrations/ivc/usage/events
    Authorization: Bearer <IVC_PUSH_TOKEN>

    {"events": [
      {"eventId":"rvc-8891","verb":"download","kind":"skill",
       "assetId":"skill-creator","eid":"EZHGMIO",
       "occurredAt":"2026-09-01T14:22:31+08:00"},
      {"eventId":"rvc-8893","verb":"like","kind":"skill",
       "assetId":"skill-creator","eid":"EPENYAN","occurredAt":1756702800}
    ]}

全部字段必填。``verb`` 是 ``download | like | unlike | open``，``kind`` 是
``skill | agent | tool``，``assetId`` 是**对方自己的** id，``occurredAt`` 收带时区 ISO 8601
或秒级 epoch。

**``open`` 是 web 型资产（``service_type=cloud``）的访问量。** 这类资产用户点开就跳到
``serviceUrl``，没有下载动作，所以"打开"是它唯一的使用信号。它只对 ``agent`` / ``tool``
成立，``open`` + ``skill`` 是 422 而不是 skipped —— 技能是下载的，那个组合重推多少次也落不了
地，标成 skipped 只会让对方一直重试。组合矩阵见
``models/external_usage_event.ALLOWED_VERB_KINDS``。

``open`` 的落点与其它动词不同：它进门户原生的 ``rp_ai_usage_event``（``verb=tool_open``，
``source=nexus-ivc``），因为那张表本来就是 ``tool_open`` 的专用落点，门户自己记的那批也在
那里 —— 进同一张表，两边口径自动一致，报告侧不用加分支。理由详见
``models/external_usage_event.VERB_OPEN``。

**``kind`` 只有一半在选表，另一半是一道一致性闸门。** ``skill`` 与其余两者决定查
``rp_ai_skill`` 还是 ``rp_ai_market_item``（两张表用同一个 ``ivc-`` 前缀，光凭 ``assetId``
分不出来）；而 ``agent`` 与 ``tool`` 落同一张表、门户侧那行上已经有 ``item_type`` 了，所以
这个区别对选表没有作用。它没被忽略，而是拿去和那行比：不一致时整条 ``skipped`` 并把两边各是
什么都写进 ``detail``。静默采纳任意一边都会出事 —— 采纳门户的，对方按 tool 统计、我们按
agent，数字对不上而两边各自自洽；采纳对方的，一次推送就能让这个条目在市场页上换一个 tab，
而没有人做过那个决定。

**为什么 ``eid`` 破例出现在请求体里。** 门户原生的 ``POST /api/usage/events`` 刻意没有这个
字段，注释写明：接受它等于让任何登录用户把行为记到同事名下。这里没有会话可取 —— token 认证
的是**系统**不是人 —— 所以只能收。三条约束把风险圈住：token 的作用域只有这一个端点、每条
事件都带 ``source`` 与门户原生行为可分离、``eid`` 解析不出就单条拒。

**为什么整批 202 而不是 204，且逐条给结果。** 一个已下架的资产不能挡住同批另外 499 条好数据，
所以单条落不了地时是 ``skipped`` 而不是把整批 4xx 掉。代价是对方只看 HTTP 状态码就会以为
全收下了 —— 这个失败形状两边都觉得正常、只有数字对不上，所以计数和逐条结果都在返回体里，
并且每条也落进 ``rp_ai_external_usage_event.action``，事后查得出来。

**落地前提：资产必须先在门户里有一行。** 两张落点表都带外键，父行不存在时这条事件物理上
插不进去，所以 ``_resolve`` 会把它标成 ``skipped`` 并说明补法。按资产形态分三种情况：

* **skill** —— 走 ``ivc_sync.py`` 的索引同步（生产已有 29 行）。
* **带包体的 agent** —— 也走那条同步（``ivc_sync._upsert_agent``），写 ``service_type=code``
  并把下载转往 Nexus（生产已有 2 行）。**这段以前写着"agent 必然 skipped"，那是过期的**：
  ``_upsert_agent`` 后来补上了，跳过 agent 的那个版本已经不在了。
* **web 型的 agent / tool**（``service_type=cloud``，用户点开就跳到 ``serviceUrl``）——
  这类此前**只能人工在界面上填表**（生产那 3 行 ``ivc-*`` 的 ``source`` 是 ``manual``、
  ``external_source`` 为空），所以新增 ``POST /api/integrations/ivc/web-assets`` 让对方自己
  登记，见 ``routers/ivc_web_assets.py``。``open`` 事件的父行就是它造出来的。

**这里是 skipped 而不是 4xx**，因为"门户还没有这个资产"是门户这边的欠账，不是对方的请求错；
补上父行之后原样重推就能落地，``eventId`` 那道唯一约束保证不会重复计数。
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import db_utc_offset, get_db
from app.models.download import RpAiSkillDownload
from app.models.external_usage_event import (
    ACTION_APPLIED,
    ACTION_DUPLICATE,
    ACTION_SKIPPED,
    KIND_AGENT,
    KIND_SKILL,
    SOURCE_RVC,
    VERB_DOWNLOAD,
    VERB_LIKE,
    VERB_OPEN,
    VERB_UNLIKE,
    RpAiExternalUsageEvent,
)
from app.models.usage_event import (
    OBJECT_MARKET_ITEM,
    VERB_TOOL_OPEN,
    RpAiUsageEvent,
)
from app.models.favorite import RpAiUserFavorite
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.market_favorite import RpAiMarketFavorite
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
# 从 ivc_sync 借这两个常量而不是照抄一份。门户侧的 id 是 `ivc-<上游 id>`、分组是 `ivc`，同步那边
# 建行用的就是它们；这里再写一遍字面量，等于埋一个「哪天改了、同步改了这边没改」的静默失配。
from app.routers.ivc_sync import ID_PREFIX, IVC_CATEGORY_ID as IVC_GROUP, IVC_SOURCE
from app.schemas.external_usage import (
    DEFAULT_FEED_LIMIT,
    MAX_EVENTS_PER_BATCH,
    MAX_FEED_LIMIT,
    ExternalUsageBatchIn,
    ExternalUsageEventIn,
    ExternalUsageEventOut,
    ExternalUsageFeed,
    ExternalUsageOutcome,
    ExternalUsageResult,
)
from app.services.dept_groups import resolve_group
from app.services.portal_users import ensure_portal_user

logger = logging.getLogger(__name__)

#: ``since`` 不给时默认回看多久。一个月：足够覆盖对方按周或按月拉一次，又不至于让默认调用
#: 扫全表。
DEFAULT_WINDOW_DAYS = 30

router = APIRouter(prefix="/api/integrations/ivc", tags=["ivc-usage"])


def require_push_token(authorization: str | None) -> None:
    """静态 bearer 闸门，形状照 ``gerrit_sync.sync``。

    **为什么是静态 token 而不是一个门户账号。** 调用方是对方机房里的一个无人值守服务：走门户的
    JWT 就得给它一个 LDAP 账号并让它交互登录，而那个账号一旦存在就能做门户里的每一件事 ——
    而它只需要往一张流水表里追加行。这个 token 的作用域正好是这一个端点。

    token 没配时是 503 并点名缺哪个变量，**不是**放行。一个「忘配就全开」的闸门，症状是没有
    症状。
    """
    token = get_settings().ivc_push_token
    if not token:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "用量推送端点未启用（IVC_PUSH_TOKEN 没有配置）",
        )
    expected = f"Bearer {token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token 缺失或不正确")


def _to_db_naive(value):
    """把带时区的时刻换成库本地时区的 naive 值。

    全仓所有时间列都是 naive、存库服务器本地时间（生产 UTC+2）。直接把对方的 ``+08:00``
    去掉时区写进去会整体偏两小时，而两小时刚好够把月底那几条挪到上个月。
    """
    return value.astimezone(timezone(db_utc_offset())).replace(tzinfo=None)


def _resolve(db: Session, kind: str, asset_id: str) -> tuple[str | None, str | None]:
    """把对方的资产 id 解析成门户侧的 id。返回 ``(resolved_id, 说不通的原因)``。

    两套口径，因为门户里这两类行的来历不同：

    * ``skill`` 走 ``ivc-<assetId>`` 主键。那些行是 ``ivc_sync`` 按对方 Nexus 索引里的 id 建的
      （``ivc_sync.py`` 的 ``f"{ID_PREFIX}{entry.id}"``），同一个 id 空间，拼前缀必然命中。
    * ``agent`` / ``tool``（web 型）走 ``external_ref``。那些行是人在界面上手工建的，主键是
      **显示名的 slug**，和对方的 id 毫无关系 —— ``ivc-globalknowledge`` 开头那个 ``ivc-`` 是
      名字「IVC-GlobalKnowledge」slug 化之后碰巧带上的，不是前缀。所以身份必须靠登记接口写下的
      ``external_ref`` 查，拼前缀在这条路上只会偶然对上。
    """
    if kind == KIND_SKILL:
        portal_id = f"{ID_PREFIX}{asset_id}"
        if db.get(RpAiSkill, portal_id) is None:
            return None, f"门户里没有技能 {portal_id}，先跑一次索引同步"
        return portal_id, None

    item = db.scalars(
        select(RpAiMarketItem).where(
            RpAiMarketItem.external_source == IVC_SOURCE,
            RpAiMarketItem.external_ref == asset_id,
        )
    ).first()
    if item is None:
        # 父行不存在时两张落点表的外键会拒,所以这里先挡下并说明补法。
        # 顺序是硬的:先登记建立身份,才能推这个资产的用量。刻意不给"按 ``ivc-<assetId>`` 再兜
        # 一次"的退路 —— 那会让少数 id 恰好撞上门户 slug 的条目偶然能推,于是"先登记"从一条硬
        # 规则变成"有时需要有时不需要",对方遇到时比直接被拒更难查。
        return None, (
            f"门户里没有登记过 {asset_id}，"
            f"先用 POST /api/integrations/ivc/web-assets 登记它再重推"
        )

    # ``agent`` 与 ``tool`` 落同一张表,所以这个区别对**选表**没有作用 —— 但它是一道免费的
    # 一致性闸门,所以不忽略它。门户侧的 ``item_type`` 是登记时写下的,对方推送时又说了一遍;
    # 两处不一致意味着有一边记错了,而两边都是"真相"的候选。
    #
    # **为什么不静默采纳任意一边。** 采纳门户的 → 对方的报表按 tool 统计、我们按 agent,数字
    # 对不上而两边各自都自洽;采纳对方的 → 一次推送就能改写条目的类型,而那会让它在市场页上
    # 换一个 tab,没有人做过这个决定。报出来让人去改其中一处,是唯一不丢信息的处理。
    if item.item_type != kind:
        return None, (
            f"类型对不上：你推的是 {kind}，门户里 {item.id} 登记成 {item.item_type}。"
            f"改一处再重推（登记走 POST /api/integrations/ivc/web-assets）"
        )
    return item.id, None


def _apply_open(
    db: Session,
    event: ExternalUsageEventIn,
    resolved_id: str,
    department: str | None,
    occurred_at,
) -> str | None:
    """把一次"打开"记进 ``rp_ai_usage_event``。

    为什么不进这张流水表自己的业务侧、而是进门户原生那张表:见
    ``models/external_usage_event.VERB_OPEN`` 的注释 —— ``tool_open`` 的专用落点本来就是
    那张表,门户自己记的那批也在那里,进同一张表两边口径自动一致。

    ``created_at`` 显式写成对方给的 ``occurredAt``,不让它走 server default。那张表只有
    ``created_at`` 一列时间,用 ``now()`` 会把对方一整夜的事件全戳在批处理那一刻,而报告是
    滚动 7 天／月窗口,边界上的归属会错。推送延迟不会因此丢掉 —— ``rp_ai_external_usage_event``
    同时存了 ``occurred_at`` 与它自己的 ``created_at``,两者之差就是延迟。
    """
    item = db.get(RpAiMarketItem, resolved_id)
    row = RpAiUsageEvent(
        eid=event.eid,
        department=department,
        verb=VERB_TOOL_OPEN,
        object_type=OBJECT_MARKET_ITEM,
        object_id=resolved_id,
        object_name=(item.name if item is not None else None),
        # 与门户自己记的那批分得开。报告要能回答"这个数里有多少是上游推来的"。
        source=SOURCE_RVC,
    )
    row.created_at = occurred_at
    db.add(row)
    return None


def _apply(
    db: Session,
    event: ExternalUsageEventIn,
    resolved_id: str,
    department: str | None,
    occurred_at,
) -> str | None:
    """改业务表。返回 ``None`` 表示改了，返回字符串表示没什么可改（附原因）。"""
    is_skill = event.kind == KIND_SKILL

    if event.verb == VERB_OPEN:
        # 组合校验已经保证 kind 不是 skill（见 schemas 的 ``_verb_fits_kind``）。
        return _apply_open(db, event, resolved_id, department, occurred_at)

    if event.verb == VERB_DOWNLOAD:
        # 下载是流水，重复的下载是真实信号（同一个人下了两次），不去重 —— 唯一下载人数由读取侧
        # 的 count(distinct eid) 负责。幂等由 eventId 那道唯一约束保证，不靠这里判重。
        row = (
            RpAiSkillDownload(skill_id=resolved_id, eid=event.eid)
            if is_skill
            else RpAiMarketDownload(item_id=resolved_id, eid=event.eid)
        )
        row.department = department
        row.created_at = occurred_at
        db.add(row)
        return None

    if is_skill:
        existing = db.scalar(
            select(RpAiUserFavorite).where(
                RpAiUserFavorite.eid == event.eid,
                RpAiUserFavorite.skill_id == resolved_id,
            )
        )
    else:
        existing = db.scalar(
            select(RpAiMarketFavorite).where(
                RpAiMarketFavorite.eid == event.eid,
                RpAiMarketFavorite.item_id == resolved_id,
            )
        )

    if event.verb == VERB_LIKE:
        if existing is not None:
            # 状态已经对了。这不是错误：对方重放一段历史、或者这个人在门户上也点过赞，
            # 都会走到这里。流水表那一行照样落下，所以「他又点了一次」这件事没丢。
            return "已经是收藏状态，只记流水"
        row = (
            RpAiUserFavorite(eid=event.eid, skill_id=resolved_id)
            if is_skill
            else RpAiMarketFavorite(eid=event.eid, item_id=resolved_id)
        )
        row.created_at = occurred_at
        db.add(row)
        return None

    # VERB_UNLIKE
    if event.verb != VERB_UNLIKE:  # 动词集是闭的，走到这里说明有人加了取值没接上分支
        raise ValueError(f"未处理的动词：{event.verb!r}")
    if existing is None:
        return "本来就不是收藏状态，只记流水"
    db.delete(existing)
    return None


@router.post(
    "/usage/events",
    response_model=ExternalUsageResult,
    status_code=status.HTTP_202_ACCEPTED,
)
def push_usage_events(
    payload: ExternalUsageBatchIn = Body(...),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ExternalUsageResult:
    """收一批上游用量事件。见模块 docstring 里的契约。"""
    require_push_token(authorization)

    if len(payload.events) > MAX_EVENTS_PER_BATCH:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"单批最多 {MAX_EVENTS_PER_BATCH} 条，这批 {len(payload.events)} 条",
        )

    result = ExternalUsageResult(received=len(payload.events))

    # 一批共用一条 LDAP 连接，且每个工号只查一次 —— 照 ``ivc_sync`` 的先例。
    #
    # 这一步不能省：``ensure_portal_user`` 在调用方不给 ``department`` 时会**无条件**查一次目录
    # （它在判断「这行存不存在」之前就查），所以不预解析的话，一批 500 条最多是 500 次 bind。
    # 目录不可用时那 500 次还各自要等一个超时，一次推送就会挂在网关上，而症状是 504、
    # 看起来像门户挂了。
    departments = _departments_for({e.eid for e in payload.events})

    for event in payload.events:
        # 每条一个 SAVEPOINT。一条坏数据（外键、并发撞唯一键）不该把同批另外 499 条一起回滚，
        # 而那正是「整批失败」在对方那边看起来像「门户挂了」的成因。
        try:
            # 每条一个 SAVEPOINT，两个作用都是承重的（各由一条测试钉住）：
            #   * 退出时 flush —— 同一批里先 like 再 unlike 同一个资产时，unlike 必须查得到刚插的
            #     那行，否则收藏行会留下来（test_like_then_unlike...）。
            #   * 出错时回滚 —— 半途写下的行必须跟着没掉，否则它会活到最后那次 commit，变成
            #     一条谁也解释不了的孤儿记录（test_an_event_that_raises_is_isolated...）。
            # 而一条坏数据回滚整批，在对方那边看起来就是「门户挂了」。
            with db.begin_nested():
                action, detail = _handle_one(db, event, departments.get(event.eid))
        except IntegrityError:
            # 并发重推：另一个请求刚插了同一个 (source, eventId)。唯一约束替我们判了重。
            action, detail = ACTION_DUPLICATE, "并发重推，已由唯一约束挡下"
        except Exception:  # noqa: BLE001
            # 单条未预料的失败也不能吃掉整批。日志留下来，返回体里如实标出。
            logger.exception("处理事件 %s 失败", event.event_id)
            action, detail = ACTION_SKIPPED, "门户内部错误，见服务端日志"

        if action == ACTION_APPLIED:
            result.applied += 1
        elif action == ACTION_DUPLICATE:
            result.duplicate += 1
        else:
            result.skipped += 1
        result.outcomes.append(
            ExternalUsageOutcome(event_id=event.event_id, action=action, detail=detail)
        )

    db.commit()
    return result


def require_read_token(authorization: str | None) -> None:
    """读回端点的闸门。**用的是另一串值**（``IVC_READ_TOKEN``），不是推送那个。

    分开的理由见 ``config.ivc_read_token``：一串同时能写又能读的值，让「只想收数据、不想再给
    数据」这个决定没有对应的动作。

    这条路径与 POST 同一个 URL，而 ``AUTH_WHITELIST`` 是**按路径**判的，所以 GET 也绕过了站点
    级 JWT —— 这里这道检查是唯一的闸门，token 没配时必须 503 而不是放行。
    """
    token = get_settings().ivc_read_token
    if not token:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "用量读回端点未启用（IVC_READ_TOKEN 没有配置）",
        )
    expected = f"Bearer {token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token 缺失或不正确")


def _ivc_only(department: str | None) -> bool:
    """这个部门算不算 IVC。

    走 ``resolve_group`` 而不是自己写一个 ``startswith("BNEW RTE RP IVC")``：那张映射表有它自己
    的规则（按段最长前缀），照抄一份就等于把规则复制到第二处，而两处不一致时没有任何一处会报错。

    具体差别不是假设，是实测的：``BNEW RTE RP CD IVC`` 与 ``CD IVC2`` 在表里**各有一条明确映射
    指向 ``ivc``**（``dept_groups.py``，2026-09 那次「按功能归属」的既定决定：成都 IVC 与北京 IVC
    并排出现在同一个 IVC 标签下）。所以自己写前缀匹配会把成都 IVC 整批漏掉，而那不报错。

    表里刻意**没有**站点级的 ``BNEW RTE RP CD`` 键（站点不是功能），所以部门恰好只到 ``RP CD``
    没有子团队的人解析为 ``None``、不算 IVC。这与部门页和月报口径一致 —— 不一致才是缺陷。
    """
    return resolve_group(department) == IVC_GROUP


def _feed_rows(db: Session, since, limit: int) -> list[ExternalUsageEventOut]:
    """按「资产是 IVC 的 **且** 操作者部门是 IVC 的」取使用记录。

    **两个条件都要，这是操作者定的最严口径。** 代价是回给上游的数字**低于真实使用量**，而且
    他们分不出「没有部门外的人用」和「有但你不告诉我」。这一点写在这里，因为下一个人看到数字
    对不上时，第一反应会是去掉这个过滤。

    **部门过滤在 Python 里做，不在 SQL 里。** ``resolve_group`` 是按段最长前缀匹配，翻成 SQL 就
    是把那张映射表复制进 where 子句。扫描量由 ``since`` 兜住，而这些表在生产上是几百行的量级。
    ``limit`` 因此**作用在部门过滤之后**，否则一页里可能全被过滤空、看起来像没有数据。
    """
    out: list[ExternalUsageEventOut] = []

    # --- 技能：下载 ---------------------------------------------------------
    # 判据用 id 前缀，与 POST 侧 ``_resolve`` 同一条规则、同一个常量。市场表上的
    # ``external_source`` 列要等另一个 MR，用前缀两类资产就能共用一条规则。
    rows = db.execute(
        select(
            RpAiSkillDownload.skill_id,
            RpAiSkillDownload.eid,
            RpAiSkillDownload.department,
            RpAiSkillDownload.created_at,
        )
        .where(
            RpAiSkillDownload.skill_id.startswith(ID_PREFIX),
            RpAiSkillDownload.created_at >= since,
        )
        .order_by(RpAiSkillDownload.created_at)
    ).all()
    for skill_id, eid, department, at in rows:
        if not _ivc_only(department):
            continue
        out.append(
            ExternalUsageEventOut(
                verb=VERB_DOWNLOAD,
                kind=KIND_SKILL,
                asset_id=skill_id[len(ID_PREFIX):],
                eid=eid,
                department=IVC_GROUP,
                occurred_at=at,
            )
        )

    # --- 技能：收藏 ---------------------------------------------------------
    # 这张表**没有 department 快照列**（与两张 download 表不同），所以部门只能从用户行现取 ——
    # 也就是「这个人现在的部门」，不是收藏当时的。一次重组会让历史收藏的归属跟着变，而下载不会。
    # 没有在这里补列：那要改一张 Grafana 也在读的表，属于另一个改动。
    rows = db.execute(
        select(
            RpAiUserFavorite.skill_id,
            RpAiUserFavorite.eid,
            RpAiPortal.department,
            RpAiUserFavorite.created_at,
        )
        .join(RpAiPortal, RpAiPortal.eid == RpAiUserFavorite.eid)
        .where(
            RpAiUserFavorite.skill_id.startswith(ID_PREFIX),
            RpAiUserFavorite.created_at >= since,
        )
        .order_by(RpAiUserFavorite.created_at)
    ).all()
    for skill_id, eid, department, at in rows:
        if not _ivc_only(department):
            continue
        out.append(
            ExternalUsageEventOut(
                verb=VERB_LIKE,
                kind=KIND_SKILL,
                asset_id=skill_id[len(ID_PREFIX):],
                eid=eid,
                department=IVC_GROUP,
                occurred_at=at,
            )
        )

    # --- 市场（agent）：下载 -------------------------------------------------
    # 今天必然是空的：agent 还没被同步进市场，所以没有 ``ivc-`` 开头的市场条目。写在这里而不是
    # 等以后再加，是因为「agent 接进来了但读回接口看不见」是一个不报错的缺口。
    rows = db.execute(
        select(
            RpAiMarketDownload.item_id,
            RpAiMarketDownload.eid,
            RpAiMarketDownload.department,
            RpAiMarketDownload.created_at,
        )
        .where(
            RpAiMarketDownload.item_id.startswith(ID_PREFIX),
            RpAiMarketDownload.created_at >= since,
        )
        .order_by(RpAiMarketDownload.created_at)
    ).all()
    for item_id, eid, department, at in rows:
        if not _ivc_only(department):
            continue
        out.append(
            ExternalUsageEventOut(
                verb=VERB_DOWNLOAD,
                kind=KIND_AGENT,
                asset_id=item_id[len(ID_PREFIX):],
                eid=eid,
                department=IVC_GROUP,
                occurred_at=at,
            )
        )

    out.sort(key=lambda e: e.occurred_at)
    return out[:limit]


@router.get("/usage/events", response_model=ExternalUsageFeed)
def read_usage_events(
    since: datetime | None = Query(
        default=None,
        description="只回这个时刻之后的记录，带时区的 ISO 8601。默认最近 30 天。",
    ),
    limit: int = Query(default=DEFAULT_FEED_LIMIT, ge=1, le=MAX_FEED_LIMIT),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ExternalUsageFeed:
    """把 IVC 资产在门户里的使用情况回给上游。

    **口径：资产是 IVC 的，且操作者部门是 IVC 的。** 非 IVC 的人对 IVC 资产的操作整条不返回 ——
    连脱敏行都不给。这是操作者定的最严口径，代价见 ``_feed_rows``。

    **返回里包含他们自己推来的那部分。** 推送写进的就是这几张业务表，读回时没法（也没必要）
    把它们摘出来 —— 他们知道自己推过哪些 ``eventId``，要扣掉是他们那边的减法。

    ``since`` 不带时区一律拒：理由与 POST 那侧同一条，裸本地时间在两个站点之间无法还原成时刻。
    """
    require_read_token(authorization)

    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=DEFAULT_WINDOW_DAYS)
    elif since.tzinfo is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "since 必须带时区偏移",
        )

    events = _feed_rows(db, _to_db_naive(since), limit)
    return ExternalUsageFeed(
        generated_at=datetime.now(timezone.utc), total=len(events), events=events
    )


def _departments_for(eids: set[str]) -> dict[str, str | None]:
    """一次把这批人的部门查出来。目录不可用时全部留空，不抛。

    留空而不是猜：目录里查不到部门时正确答案是空。编一个值会在报告里变成一个事后没人能判断
    对错的数字。而查目录失败也不该让整批推送失败 —— 部门是可以下次经过时补上的
    （``ensure_portal_user`` 只补空缺、不覆盖），用量数据丢了就是丢了。
    """
    try:
        from app.services.asset_owner import DirectoryDepartments

        directory = DirectoryDepartments()
        try:
            return {eid: directory.department_of(eid) for eid in eids}
        finally:
            directory.close()
    except Exception:  # noqa: BLE001
        logger.warning("查目录失败，这批 %d 个工号的部门留空", len(eids), exc_info=True)
        return {}


def _handle_one(
    db: Session, event: ExternalUsageEventIn, department: str | None
) -> tuple[str, str | None]:
    """一条事件：判重 -> 建人 -> 解析资产 -> 改业务表 -> 落流水。"""
    # **承重的是流水表上那道唯一约束，不是这句判重。** 反面探针证明的：把这句删掉，全部测试照样
    # 绿 —— 因为落流水时 flush 会抛 IntegrityError，外层接住、SAVEPOINT 回滚，业务表一个字没改。
    # 这句留着是快路径（省掉做完再回滚的那一趟，也省掉日志里一条噪声异常），不是保证。
    # 保证由 ``uq_ext_usage_source_event`` 提供，并由 test_the_ledger_has_the_unique_constraint
    # 钉住 —— 那道约束一旦被谁删掉，翻倍就会静默发生。
    if db.scalar(
        select(RpAiExternalUsageEvent.id).where(
            RpAiExternalUsageEvent.source == SOURCE_RVC,
            RpAiExternalUsageEvent.event_id == event.event_id,
        )
    ):
        return ACTION_DUPLICATE, "eventId 已入库"

    occurred_at = _to_db_naive(event.occurred_at)

    # 收藏表的 eid 是指向 rp_ai_portal 的外键，而推来的人可能从未登录过门户。这里必须先建行，
    # 且部门要一起补上 —— 列是空的就等于这个人不属于任何部门，他名下的量进「无法归属」。
    #
    # ``lookup_directory=False``：部门上面已经按批查过了，这里再查一次就是每条一次 bind。
    # ``origin='rvc-push'`` 而不是 ``'login'``：这些人没登录过门户，算进「曾登录过的人数」
    # 会虚增渗透率。真登录时 ``auth.py`` 会把它改写成 ``'login'``。
    user = ensure_portal_user(
        db,
        event.eid,
        department=department,
        origin="rvc-push",
        lookup_directory=False,
    )
    department = user.department

    resolved_id, why_not = _resolve(db, event.kind, event.asset_id)
    detail = why_not
    action = ACTION_SKIPPED
    if resolved_id is not None:
        detail = _apply(db, event, resolved_id, department, occurred_at)
        action = ACTION_APPLIED

    db.add(
        RpAiExternalUsageEvent(
            source=SOURCE_RVC,
            event_id=event.event_id,
            verb=event.verb,
            kind=event.kind,
            asset_id=event.asset_id,
            resolved_id=resolved_id,
            eid=event.eid,
            department=department,
            occurred_at=occurred_at,
            action=action,
            detail=detail,
        )
    )
    return action, detail
