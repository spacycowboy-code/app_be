"""把 IVC 的 Nexus 索引贴进来，先看清它有什么，再决定同步什么。

**为什么是粘贴。** 索引在 ``arm1s10-eiffel239.eiffel.gic.ericsson.se:8443``。实测生产宿主机到
那台机器 ICMP 通（0% 丢包、5.7ms）而 **TCP 22 / 80 / 443 / 8081 / 8443 全部静默超时**，容器与
宿主机都没有代理配置 —— 所以这不是端口写错或该走代理没走，是整段 TCP 被拦。能访问它的是员工
工位的浏览器，于是入口反过来：由人把 JSON 贴进门户。

**为什么这一步只出方案不写库。** 索引里的 ``download_url`` 全部指向那台不可达的 Nexus，门户
拿不到包体，也就无法把包推进 GitLab。目录（名字、描述、作者、版本、标签、链接）拿得到，包体
拿不到。所以"同步"这个词在这里对两件事只成立一件，把它们做在同一个按钮后面，操作者会以为
资产已经完整进来了。这个端点回答的是"这份索引里有什么、哪几条有问题、入库会发生什么"。

**谁能用。** 复用 ``dashboard_eids``（见 ``app.config``；这里不抄一份名单出来，抄了就会跟着漂）而不是另立一个白名单
—— 那四个人已经是能看整份部门使用报告（含每个人的姓名和用量）的人，再给他们一个只读的索引
预览不扩大任何权限面。判定放在服务端：前端把入口藏起来不是安全边界，这个端点公开可达，绕过
界面直接 POST 一样能到。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.category import RpAiCategory
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.schemas.common import CamelModel
from app.services import ivc_index
from app.services.asset_owner import DirectoryDepartments

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations/ivc", tags=["ivc-sync"])


def require_sync_eid(request: Request) -> str:
    """只放行 ``dashboard_eids`` 里的账号。

    照抄 ``usage_report.require_dashboard_eid``：站点级鉴权已经挡掉匿名调用者，所以走到这里
    意味着会话有效，但不一定被允许。
    """
    eid = str(getattr(request.state, "eid", "") or "").strip().upper()
    if not eid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录。")
    if eid not in get_settings().dashboard_eid_list:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "你没有同步 IVC 索引的权限。")
    return eid


class IvcEntryPlan(CamelModel):
    """一条资产在这份索引里的样子，以及入库前挡下它的理由（如果有）。"""

    kind: str
    id: str
    name: str
    version: str
    description: str
    tags: list[str] = []
    download_url: str
    #: 上游给的身份原文：skill 是 ``author``，agent 是 ``owner``。
    raw_owner: str
    #: 归一化后的工号；agent 的 owner 映射不出来时为空。
    eid: str | None = None
    display_name: str | None = None
    runtime: str | None = None
    #: 空表示这一条可以入库。
    problems: list[str] = []


class IvcIndexPlan(CamelModel):
    total: int
    usable: int
    rejected: int
    #: 索引生成时间（不是资产创建时间），拿不到时为空。
    generated_at: str | None = None
    #: 整份索引层面的观察，逐条呈现给操作者。
    notes: list[str] = []
    entries: list[IvcEntryPlan] = []


class IvcSyncOutcome(CamelModel):
    """一条资产同步后的结果。"""

    kind: str
    id: str
    name: str
    #: created | updated | unchanged | skipped
    action: str
    #: skipped 的原因，或 updated 时改了什么。
    detail: str = ""


class IvcSyncResult(CamelModel):
    created: int
    updated: int
    unchanged: int
    skipped: int
    notes: list[str] = []
    outcomes: list[IvcSyncOutcome] = []


@router.post(
    "/preview",
    response_model=IvcIndexPlan,
    summary="解析粘贴进来的 IVC 索引并给出同步方案（不写库）",
)
def preview_index(
    payload: object = Body(...),
    actor: str = Depends(require_sync_eid),
) -> IvcIndexPlan:
    """检查一份索引，返回逐条方案。**不修改任何数据。**

    校验失败时返回 400 并说明整份索引哪里不对；单条有问题不影响其余条目 —— 报告那一条，让
    操作者看到其余仍然可用，比整批拒收有用。
    """
    try:
        parsed = ivc_index.parse(payload)
    except ivc_index.IndexRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    logger.info(
        "IVC index previewed by %s: %d entries, %d usable",
        actor,
        len(parsed.entries),
        len(parsed.usable),
    )
    return IvcIndexPlan(
        total=len(parsed.entries),
        usable=len(parsed.usable),
        rejected=len(parsed.rejected),
        generated_at=(
            parsed.generated_at.isoformat() if parsed.generated_at else None
        ),
        notes=parsed.notes,
        entries=[
            IvcEntryPlan(
                kind=e.kind,
                id=e.id,
                name=e.name,
                version=e.version,
                description=e.description,
                tags=e.tags,
                download_url=e.download_url,
                raw_owner=e.raw_owner,
                eid=e.eid,
                display_name=e.display_name,
                runtime=e.runtime,
                problems=e.problems,
            )
            for e in parsed.entries
        ],
    )


#: 同步进来的技能落在这个分组下。生产库里 ``ivc`` 已经存在（``a-replir-allocation-tool`` 就挂
#: 在它下面），所以不新建分组 —— 多一个分组等于在市场的筛选栏里多一个别人要理解的东西。
IVC_CATEGORY_ID = "ivc"

#: ``external_source`` 的取值。写死一个常量而不是每处拼字符串：报告要按它把这批资产择出来。
IVC_SOURCE = "nexus-ivc"

#: 门户自己的 id 前缀。不直接用上游 id，因为 ``skill-creator`` 与门户已有的
#: ``anthropics-skill-creator``（name 也叫 ``skill-creator``）会在市场里并排出现两张同名卡片。
#: 加前缀之后 id 不会撞，卡片上靠作者区分 —— 这是操作者定的口径：重名没关系，上传人不一样就行。
ID_PREFIX = "ivc-"


@router.post(
    "/sync",
    response_model=IvcSyncResult,
    summary="把粘贴进来的 IVC 索引登记进技能市场（下载转往 Nexus）",
)
def sync_index(
    payload: object = Body(...),
    actor: str = Depends(require_sync_eid),
    db: Session = Depends(get_db),
) -> IvcSyncResult:
    """登记目录，**不搬内容**。

    门户拿不到包体（Nexus 对生产机房整段 TCP 不可达），所以这里写下的是"这个技能存在、它在
    哪里、是谁的"，下载时由 ``skills.download_skill`` 先记事件再 307 转到 Nexus —— 用户的浏览器
    在工位网段上能访问它。防火墙放行之后这批行可以原地补上 ``storage_path``/``repo_commit``
    改成门户自己存包，不需要重建。

    体检不通过的条目**不入库**，并在结果里说明理由。其中 agent 那一条是已知会被挡下的：它的
    ``owner`` 是 ``wenjie.a.liu`` 而报告只认工号，映射表补上之后它才会进来。
    """
    try:
        parsed = ivc_index.parse(payload)
    except ivc_index.IndexRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if db.get(RpAiCategory, IVC_CATEGORY_ID) is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"分组「{IVC_CATEGORY_ID}」不存在，先建分组再同步。",
        )

    outcomes: list[IvcSyncOutcome] = []
    counts = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}

    # 一次同步共用一条 LDAP 连接。放在 try/finally 里而不是循环里各开各的:一条索引可能有
    # 十几个作者,每人一次 bind 既慢又会在目录侧看起来像在扫号。
    directory = DirectoryDepartments()
    try:
        _run_sync(db, parsed, directory, outcomes, counts)
    finally:
        directory.close()

    db.commit()
    logger.info(
        "IVC index synced by %s: created=%d updated=%d unchanged=%d skipped=%d",
        actor, counts["created"], counts["updated"], counts["unchanged"],
        counts["skipped"],
    )
    return IvcSyncResult(**counts, notes=parsed.notes, outcomes=outcomes)


def _run_sync(
    db: Session,
    parsed: ivc_index.ParsedIndex,
    directory: DirectoryDepartments,
    outcomes: list[IvcSyncOutcome],
    counts: dict[str, int],
) -> None:
    """逐条登记。抽出来只是为了让上面那个 ``finally`` 覆盖整段循环。"""
    for entry in parsed.entries:
        if not entry.usable:
            counts["skipped"] += 1
            outcomes.append(
                IvcSyncOutcome(
                    kind=entry.kind, id=entry.id, name=entry.name,
                    action="skipped", detail="；".join(entry.problems),
                )
            )
            continue

        if entry.kind != "skill":
            # agent 走的是包上传管线（建 GitLab 项目、推送、打 tag），而那条路要包体。等防火墙
            # 放行、或改成连包一起上传时再接，现在明确跳过而不是勉强登记成一个技能。
            counts["skipped"] += 1
            outcomes.append(
                IvcSyncOutcome(
                    kind=entry.kind, id=entry.id, name=entry.name, action="skipped",
                    detail="agent 走包上传管线，需要包体，等 Nexus 可达后再同步",
                )
            )
            continue

        sid = f"{ID_PREFIX}{entry.id}"
        skill = db.get(RpAiSkill, sid)
        if skill is None:
            skill = RpAiSkill(
                id=sid,
                name=entry.name,
                status="approved",
                category_id=IVC_CATEGORY_ID,
                tags=entry.tags,
            )
            db.add(skill)
            action, detail = "created", ""
        else:
            changed = []
            if skill.version != entry.version:
                changed.append(f"版本 {skill.version} -> {entry.version}")
            if skill.external_url != entry.download_url:
                changed.append("下载地址")
            if skill.description != entry.description:
                changed.append("描述")
            action = "updated" if changed else "unchanged"
            detail = "；".join(changed)

        skill.description = entry.description
        skill.version = entry.version
        skill.tags = entry.tags
        # author 存工号(与门户其余技能一致,人名由 creator_label 在读取时解析);
        # uploader_eid 是外键,指向用户表 —— 8 位作者里有 5 个从未登录过门户,所以先补一行。
        skill.author = entry.eid
        skill.uploader_eid = _ensured_user(
            db, entry.eid, entry.display_name, directory.department_of(entry.eid)
        )
        skill.external_url = entry.download_url
        skill.external_source = IVC_SOURCE
        # 内容不在我们的仓库里,这两个字段必须留空 —— 填了会让下载路径去读不存在的字节。
        skill.storage_path = None
        skill.repo_commit = None

        counts[action] += 1
        outcomes.append(
            IvcSyncOutcome(
                kind=entry.kind, id=sid, name=entry.name, action=action, detail=detail
            )
        )

def _ensured_user(
    db: Session,
    eid: str,
    display_name: str | None,
    department: str | None = None,
) -> str:
    """保证用户表里有这个人,并返回工号。

    ``uploader_eid`` 是指向 ``rp_ai_portal`` 的外键,而索引里 8 位作者有 5 个从未登录过门户。
    不补这一行,同步会因为外键失败;补一个不带人名的空行,卡片上就只能显示工号。上游给了
    ``display_name``,用它。

    ``department`` 是后来补上的,因为漏写它造成了一个不报错的缺口:报告与部门页都按这一列
    归属,列是空的就等于这个人不属于任何部门,他名下的资产进「无法归属」。实测是 5 个真人、
    10 件 IVC 技能,而这 5 个人在目录里**都有** IVC 的部门 —— 数据一直拿得到,只是这里没写。
    对照 ``services/gerrit_assets._ensure_user`` 是写的,两条同步路径此前不对称。

    ``origin="import"`` 是必须显式写的。这一列的默认值是 ``login``,因为在它加上来之前这张表
    的每一行都确实是首次登录时建的 —— 而这里正是打破那个等价的地方:这五个人从未登录过门户,
    他们的行只是为了把 IVC 的技能挂到一个部门、一个组徽章上。不写这个值,报告里「曾登录过的
    人数」会凭空多五个人,而这恰好是 rp_ai_portal.origin 这一列被加上来要防的事
    (见 models/user.py::PORTAL_ROW_ORIGINS)。他们哪天真的登录了,``auth._upsert_user`` 会把
    这一行提升成 ``login``,所以这里写 import 不会永久把人关在渗透率之外。
    """
    row = db.query(RpAiPortal).filter(RpAiPortal.eid == eid).first()
    if row is None:
        db.add(
            RpAiPortal(
                eid=eid,
                display_name=display_name or eid,
                department=(department or "")[:64] or None,
                origin="import",
            )
        )
        db.flush()
        return eid

    # 已有的行不动 origin:它可能是真的登录建的,导入不该把它降级。
    if display_name and (not row.display_name or row.display_name == eid):
        row.display_name = display_name
    # 只补空缺,不覆盖。覆盖会让一次同步把目录快照盖在一条更新的登录记录上,而登录那条是
    # 这个人自己带来的、比快照新。反过来「只补空」让这一次同步就把历史欠的 5 行补齐,不需要
    # 另写回填脚本 —— 与 gerrit_assets._ensure_user 同一形状。
    if not row.department and department:
        row.department = department[:64]
    return eid
