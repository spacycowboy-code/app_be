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

全部字段必填。``verb`` 是 ``download | like | unlike``，``kind`` 是 ``skill | agent``，
``assetId`` 是**对方自己的** id，``occurredAt`` 收带时区 ISO 8601 或秒级 epoch。

**为什么 ``eid`` 破例出现在请求体里。** 门户原生的 ``POST /api/usage/events`` 刻意没有这个
字段，注释写明：接受它等于让任何登录用户把行为记到同事名下。这里没有会话可取 —— token 认证
的是**系统**不是人 —— 所以只能收。三条约束把风险圈住：token 的作用域只有这一个端点、每条
事件都带 ``source`` 与门户原生行为可分离、``eid`` 解析不出就单条拒。

**为什么整批 202 而不是 204，且逐条给结果。** 一个已下架的资产不能挡住同批另外 499 条好数据，
所以单条落不了地时是 ``skipped`` 而不是把整批 4xx 掉。代价是对方只看 HTTP 状态码就会以为
全收下了 —— 这个失败形状两边都觉得正常、只有数字对不上，所以计数和逐条结果都在返回体里，
并且每条也落进 ``rp_ai_external_usage_event.action``，事后查得出来。

**已知落地缺口：``kind": "agent"`` 今天必然 skipped。** ``ivc_sync.py`` 明确跳过所有 agent
（那条路要包体），所以门户库里没有任何 RVC 侧的 agent 资产，而两张落点表都带外键，父行不存在
时这条事件物理上插不进去。不是把它做成 4xx，因为这是门户这边的欠账，不是对方的请求错。
"""
from __future__ import annotations

import hmac
import logging
from datetime import timezone

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
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
    KIND_SKILL,
    SOURCE_RVC,
    VERB_DOWNLOAD,
    VERB_LIKE,
    VERB_UNLIKE,
    RpAiExternalUsageEvent,
)
from app.models.favorite import RpAiUserFavorite
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.market_favorite import RpAiMarketFavorite
from app.models.skill import RpAiSkill
# 从 ivc_sync 借这个前缀而不是照抄一份。门户侧的 id 是 `ivc-<上游 id>`，同步那边建行用的就是
# 这个常量；这里再写一遍字面量，等于埋一个「哪天前缀改了、同步改了这边没改」的静默失配。
from app.routers.ivc_sync import ID_PREFIX
from app.schemas.external_usage import (
    MAX_EVENTS_PER_BATCH,
    ExternalUsageBatchIn,
    ExternalUsageEventIn,
    ExternalUsageOutcome,
    ExternalUsageResult,
)
from app.services.portal_users import ensure_portal_user

logger = logging.getLogger(__name__)

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
    """把对方的资产 id 解析成门户侧的 id。返回 ``(resolved_id, 说不通的原因)``。"""
    portal_id = f"{ID_PREFIX}{asset_id}"
    if kind == KIND_SKILL:
        if db.get(RpAiSkill, portal_id) is None:
            return None, f"门户里没有技能 {portal_id}，先跑一次索引同步"
        return portal_id, None
    if db.get(RpAiMarketItem, portal_id) is None:
        # 见模块 docstring：这是门户这边的欠账，agent 从来没被同步进来过。
        return None, f"门户里没有市场条目 {portal_id}（agent 尚未接入同步）"
    return portal_id, None


def _apply(
    db: Session,
    event: ExternalUsageEventIn,
    resolved_id: str,
    department: str | None,
    occurred_at,
) -> str | None:
    """改业务表。返回 ``None`` 表示改了，返回字符串表示没什么可改（附原因）。"""
    is_skill = event.kind == KIND_SKILL

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
