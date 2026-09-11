"""Skill endpoints: browse, detail, create, update, delete."""
import hashlib
import io
import logging
import re
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.favorite import RpAiUserFavorite
from app.models.category import RpAiCategory
from app.models.skill import (
    RpAiInputParam,
    RpAiSkill,
    RpAiSkillCase,
    RpAiSubSkill,
)
from app.models.version import RpAiSkillVersion
from app.models.download import RpAiSkillDownload
from app.services.download_counts import SKILL, unique_downloads
from app.repositories import skill_content
from app.schemas.skill import (
    ExternalDownloadRead,
    SkillCreate,
    SkillImageState,
    SkillRead,
    SkillSummary,
    SkillUpdate,
)
from app.services.skill_manifest import (
    build_manifest,
    has_name_and_description,
    parse_frontmatter,
    with_frontmatter,
)
from app.services.archives import common_top_dir
from app.services.asset_stats import (
    attach_skill_groups,
    attach_skill_stats,
    content_updated_at,
)
from app.services.skill_storage import sanitize_segment, skill_storage
from app.services import gitlab_mr
from app.services import asset_content
from app.services import reporting_windows
from app.services import skill_image
from app.services.audit_engine import AuditResult, Finding as EngineFinding, audit_skill
from app.services.audit_report import build_checks
from app.services.skill_audit import gate
from app.services.skill_audit import pending as pending_updates
from app.services.skill_audit.reconcile import record_for_published
from app.schemas.audit import AuditCheck, AuditFinding, SkillAuditReport
from fastapi.concurrency import run_in_threadpool
from app.services.notify import notify_version_update
from app.services.eids import acting_eid, normalize_eid
from app.services.usage import department_of

router = APIRouter(prefix="/api/skills", tags=["skills"])
settings = get_settings()
logger = logging.getLogger(__name__)


def _content_present_on_branch(storage_path: str) -> bool:
    """这个技能的内容现在还在发布分支上吗。

    按**分支**读,不是按技能行上记着的 ``repo_commit`` —— 问题恰恰是那一格可能已经过期。

    只有下架用得到:它需要把"上一次下架把文件删掉了"和"有人并发改了同一个技能"分开,而这两种
    情况在 ``publish`` 那里都表现成同一个 ``Conflict``。

    上游问不出来时返回 ``True``(当成"还在")。这是失败关闭的方向:下架会照旧抛 409 让人重试,
    而反过来把 GitLab 一次抽风当成"内容已经没了",会在仓库里还有内容的情况下把行标成已下架。
    """
    try:
        skill_content.read_tree(settings.skill_repo_branch, storage_path)
        return True
    except skill_content.NotFound:
        return False
    except skill_content.ContentUnavailable as exc:
        logger.warning("问不出 %s 在分支上的状态,按'还在'处理:%s", storage_path, exc)
        return True


def _read_skill_md(skill: RpAiSkill) -> str:
    """按这一行记着的引用读 SKILL.md。

    **读不到就 503,不回落到 `skill.instructions`。** 那一列是同一份正文的第二份副本,
    回落等于把可能过期的内容当成当前版本拿去编辑或审计,而且从响应上看不出来。
    生产 22 个技能的 `storage_path` 与 `repo_commit` 都有值,所以这条回落对现存数据从不生效。

    内容住在哪个上游由 `asset_content` 按 `storage_path` 的前缀分派 —— 从 Gerrit 同步来的
    技能内容在别人的仓里,而这里的调用方不需要知道这件事。
    """
    if not skill.storage_path or not skill.repo_commit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="这个技能没有记下内容在仓库里的位置或版本,无法读取正文",
        )
    try:
        return asset_content.read_text(skill.storage_path, skill.repo_commit, "SKILL.md")
    except asset_content.ContentUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="暂时读不到技能内容,请稍后重试",
        ) from exc


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or uuid.uuid4().hex[:12]


def _get_skill_or_404(db: Session, skill_id: str) -> RpAiSkill:
    skill = db.get(RpAiSkill, skill_id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Skill '{skill_id}' not found")
    return skill


def _apply_children(skill: RpAiSkill, payload) -> None:
    """Replace child collections from a create/update payload when provided."""
    if getattr(payload, "cases", None) is not None:
        skill.cases = [
            RpAiSkillCase(title=c.title, image=c.image) for c in payload.cases
        ]
    if getattr(payload, "sub_skills", None) is not None:
        skill.sub_skills = [
            RpAiSubSkill(
                name=s.name,
                description=s.description,
                badge=s.badge,
                icon_color=s.icon_color,
                icon_bg=s.icon_bg,
            )
            for s in payload.sub_skills
        ]
    if getattr(payload, "input_params", None) is not None:
        skill.input_params = [
            RpAiInputParam(
                name=p.name,
                type=p.type,
                required=p.required,
                description=p.description,
            )
            for p in payload.input_params
        ]


@router.get("", response_model=list[SkillSummary])
def list_skills(
    db: Session = Depends(get_db),
    category_id: str | None = Query(None, alias="categoryId"),
    status_filter: str | None = Query(None, alias="status"),
    q: str | None = Query(None, description="search in name/description"),
    trending: bool | None = Query(None),
    x_user_eid: str | None = Header(None),
) -> list[RpAiSkill]:
    stmt = select(RpAiSkill)
    if category_id and category_id != "all":
        stmt = stmt.where(RpAiSkill.category_id == category_id)
    if status_filter:
        stmt = stmt.where(RpAiSkill.status == status_filter)
    else:
        # 下架过的不在列表里 —— **包括管理员和上传者本人**。下架这个动作承诺的就是"从市场
        # 消失"(见 `delete_skill` 的注释)，而下面那道可见性过滤只挡住别人:管理员走的是不加
        # 过滤的分支，上传者本人命中 `uploader_eid == requester`，于是他们两个人的市场页里
        # 会留着一个自己刚下架的技能。2026-09-09 生产上这两个人拿到的其实是 500,因为
        # `SkillStatus` 当时还不认识 `archived`;字面量补上之后,那一行会变成"下架了却还在
        # 市场上",比 500 更难发现。
        #
        # 显式问 `?status=archived` 仍然给得出来:那是一次刻意的查询,不是市场页的默认视图。
        stmt = stmt.where(RpAiSkill.status != "archived")
    if trending is not None:
        stmt = stmt.where(RpAiSkill.is_trending.is_(trending))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            RpAiSkill.name.like(like) | RpAiSkill.description.like(like)
        )
    # Visibility is gated by the lifecycle field only: a skill awaiting review
    # (or rejected by it) is not public. The audit verdict is evidence for why,
    # not the gate. Admin sees everything and an uploader still sees their own.
    # (A danger verdict is never stored at all.)
    requester = normalize_eid(x_user_eid)
    if requester != settings.audit_admin_eid:
        stmt = stmt.where(
            (RpAiSkill.status == "approved")
            | (RpAiSkill.uploader_eid == requester)
        )
    skills = list(db.scalars(stmt).all())
    # 组由 attach_skill_stats 一并挂上（services/asset_stats.attach_skill_groups）。
    # 这里曾经还有一句 _attach_group(db, skills)：同一个缺陷被两个人各修一次，一个补了调用、
    # 一个把实现收成一处并删掉了旧函数，两个都通过了自己的 CI，合到一起就成了对已删符号的
    # 调用 —— GET /api/skills 直接 500。tools/check_undefined_names.py 能抓到它，但 CI 跑的
    # 是各自合并前的基线。
    attach_skill_stats(db, skills)
    return skills



@router.get("/{skill_id}", response_model=SkillRead)
def get_skill(skill_id: str, db: Session = Depends(get_db)) -> SkillRead:
    skill = _get_skill_or_404(db, skill_id)
    skill.favorite_count = db.scalar(
        select(func.count())
        .select_from(RpAiUserFavorite)
        .where(RpAiUserFavorite.skill_id == skill_id)
    ) or 0
    upload_count, latest_upload = db.execute(
        select(
            func.count(RpAiSkillVersion.id),
            func.max(RpAiSkillVersion.created_at),
        ).where(RpAiSkillVersion.skill_id == skill_id)
    ).one()
    skill.content_updated_at = content_updated_at(
        skill, int(upload_count or 0), latest_upload
    )
    # 这条端点自己算统计、不走 ``attach_skill_stats``（那是列表的路），所以去重也得在这里
    # 显式做一次。漏了它的后果是详情页和列表卡片对同一个技能显示两个不同的下载量,而两个
    # 数都"看起来正常",没人会想到去对。
    skill.downloads = int(unique_downloads(db, SKILL, [skill_id]).get(skill_id, 0))
    attach_skill_groups(db, [skill])
    # 正文按引用从仓库读,不发数据库里那一列 —— 同一份正文两个来源,而响应上看不出用了哪一份。
    #
    # 先把模型构出来再改字段,不给 ORM 对象赋值:`instructions` 是真列,赋值会把 session 弄脏,
    # 有可能被写回数据库。
    #
    # 读不到就 503,和 `/markdown` 一致:这时候页面本来也显示不出正文(它走的就是 `/markdown`),
    # 而把这一格填成空会让复核界面把"读不到"算成"这个技能没写文档"。
    out = SkillRead.model_validate(skill)
    out.instructions = _read_skill_md(skill) if skill.storage_path else None
    return out


@router.get("/{skill_id}/markdown")
def get_skill_markdown(skill_id: str, db: Session = Depends(get_db)) -> dict:
    """Return the skill's full SKILL.md content (the source of truth)."""
    skill = _get_skill_or_404(db, skill_id)
    return {"skillId": skill.id, "content": _read_skill_md(skill)}


@router.get("/{skill_id}/audit", response_model=SkillAuditReport)
def get_skill_audit(skill_id: str, db: Session = Depends(get_db)) -> SkillAuditReport:
    """当前发布版本的审计结论,给详情页只读展示。

    读的是**已发布内容**对应的那条审计记录(``record_for_published``,而不是"最近一次审计"),
    因为详情页展示的就是发布出去的那一版 —— 一次被拦下的、还没发布的更新不该出现在这里。

    逐条发现按类别归并成 checks(和上传向导同一套 ``build_checks`` 逻辑,展示才一致),但**去掉
    ``evidence`` 原文**:这是给任何看得到该技能的人看的,而证据片段可能牵出实现或扫描规则细节。

    发现按严重级分两组:critical/high 进 ``checks``(把技能推向人工复核的"需关注项");medium 及以下
    进 ``low_risk_checks``("已放行的低危项" —— 扫到了但不影响放行)。degraded(某层未跑完)的检查项
    归入需关注组,因为"没核实"也是复核的理由。

    没有审计记录(老数据/从未审计)时 ``available=False``,前端显示"未审计"而不是"通过"。
    """
    skill = _get_skill_or_404(db, skill_id)
    record = record_for_published(db, skill)
    if record is None:
        return SkillAuditReport(available=False)

    high = {"critical", "high"}
    concern_findings = [f for f in record.findings if (f.severity or "").lower() in high]
    low_findings = [f for f in record.findings if (f.severity or "").lower() not in high]

    return SkillAuditReport(
        available=True,
        state=record.state,
        reason=record.reason or "",
        coverage=record.coverage or "",
        # 需关注组带上 incomplete,好让 build_checks 生成 degraded 项(某层未跑完也要复核)。
        checks=_checks_from_findings(
            concern_findings,
            incomplete=[s for s in (record.incomplete or "").split(",") if s],
        ),
        low_risk_checks=_checks_from_findings(low_findings, incomplete=[]),
    )


def _checks_from_findings(
    db_findings: "list", *, incomplete: "list[str]"
) -> "list[AuditCheck]":
    """把库里的 findings 按类别归并成 AuditCheck,复用 build_checks 的归并与排序。

    从库行重建一个 AuditResult 走同一套逻辑(展示才和上传向导一致,不另造归并)。**evidence 原文
    刻意不带出来** —— 见 SkillAuditReport 的说明。
    """
    result = AuditResult(
        incomplete=incomplete,
        findings=[
            EngineFinding(
                source=f.source, category=f.category, severity=f.severity,
                evidence=f.evidence, detail=f.detail, file=f.file, line=f.line,
                rule_id=f.rule_id or "", aitech=f.aitech, remediation=f.remediation,
            )
            for f in db_findings
        ],
    )
    return [
        AuditCheck(
            id=c.id, label=c.label, status=c.status, description=c.description,
            severity=c.severity, note=c.note,
            findings=[
                AuditFinding(
                    source=f.source, category=f.category, severity=f.severity,
                    evidence=None, detail=f.detail, file=f.file, line=f.line,
                    rule_id=f.rule_id or None, aitech=f.aitech, remediation=f.remediation,
                )
                for f in c.findings
            ],
        )
        for c in build_checks(result)
    ]


def _artifact_member(tree: "dict[str, bytes]") -> "bytes | None":
    """作者上传的那个原始 zip(在 ``artifacts/`` 下),没有就返回 None。

    "哪个文件算作者的原始包"是产品规则,不是存储事实,所以留在路由层:仓储只负责把这一版的文件
    交出来,不判断它们各自是什么角色。
    """
    zips = sorted(k for k in tree if k.startswith("artifacts/") and k.endswith(".zip"))
    return tree[zips[0]] if zips else None


def _common_top_dir(members: list[zipfile.ZipInfo]) -> "str | None":
    """The single top-level directory shared by every archive member, else
    ``None``. Used to strip a redundant wrapper folder (e.g.
    ``tavily-web-search/``) so the download isn't double-nested.

    Delegates to ``services.archives`` so this rule has one implementation: the
    market download endpoint applies the same rule to GitLab's
    ``<project>-<ref>-<sha>`` wrapper, and two copies would drift."""
    return common_top_dir(members)


def _bump_downloads(db: Session, skill: RpAiSkill, eid: str | None = None) -> None:
    """Record a download: log a timestamped event row (with the downloader's
    EID when known, for per-user leaderboards) and increment the cumulative
    counter. Best-effort — a logging failure must never break the download.

    A download is NOT a modification of the skill, so this must not move
    ``updated_at``. That column carries ``onupdate=func.now()`` (TimestampMixin),
    which fires on any UPDATE that does not name it — and the Home page's
    "What's New" feed reports a skill as updated whenever
    ``updated_at > created_at``, so bumping the counter through the ORM made
    every download surface as a bogus "Update" card with an unchanged version.
    Naming ``updated_at`` in the SET clause suppresses ``onupdate`` and carries
    the existing value forward, so only real edits move it.
    """
def _bump_downloads(db: Session, skill: RpAiSkill, eid: str | None = None) -> None:
    """Record a download: log a timestamped event row (with the downloader's
    EID when known, for per-user leaderboards) and increment the cumulative
    counter. Best-effort — a logging failure must never break the download.

    A download is NOT a modification of the skill, so this must not move
    ``updated_at``. That column carries ``onupdate=func.now()`` (TimestampMixin),
    which fires on any UPDATE that does not name it — and the Home page's
    "What's New" feed reports a skill as updated whenever
    ``updated_at > created_at``, so bumping the counter through the ORM made
    every download surface as a bogus "Update" card with an unchanged version.
    Naming ``updated_at`` in the SET clause suppresses ``onupdate`` and carries
    the existing value forward, so only real edits move it.

    The increment is done **in SQL** (``downloads + 1``), not read-modify-write
    in Python. The old code read the counter, parsed it, added one and wrote the
    result back; two downloads landing together both read ``N`` and both wrote
    ``N + 1``, so one of them was lost. Letting the database do the addition
    makes it atomic, and it is only expressible this way because the column is
    now an integer.

    The event row is written and committed **before** the counter, in its own
    transaction. The two are not equally important: ``rp_ai_skill_download`` is
    the record of what happened and is what the inventory and the department
    reports count, while ``downloads`` is a cache of it. Putting both in one
    transaction meant a failed counter update rolled the event row back too —
    losing the truth because the cache failed.
    """
    downloader = (eid or "").strip() or None
    try:
        db.add(
            RpAiSkillDownload(
                skill_id=skill.id,
                eid=downloader,
                department=department_of(db, downloader),
            )
        )
        db.commit()
    except Exception:
        db.rollback()
    try:
        # **写去重后的值,不是自增。** 下载量的定义已经是"有多少个不同的人下载过",而 +1
        # 数的是次数 —— 同一个人下十次会把这一列推到 10,而页面显示的是 1。
        #
        # 而且这里刻意是"算出来覆盖"而不是"判断这个人是不是新的再 +1":后者有竞态(两个并发
        # 的首次下载都看不到对方的行,双双 +1),而覆盖式没有 —— 事件行在上面那个 try 里已经
        # 提交了,所以这一句算出来的就是含本次在内的真值。它还是**自愈**的:任何历史漂移会在
        # 这个技能下一次被下载时自动归位。
        fresh = unique_downloads(db, SKILL, [skill.id]).get(skill.id, 0)
        db.execute(
            update(RpAiSkill)
            .where(RpAiSkill.id == skill.id)
            .values(downloads=fresh, updated_at=RpAiSkill.updated_at)
        )
        # The Core UPDATE bypassed the ORM; drop the now-stale attributes so
        # a later read in this session sees the stored values.
        db.expire(skill, ["downloads", "updated_at"])
        db.commit()
    except Exception:
        db.rollback()


def _content_disposition(filename: str) -> str:
    """Build an attachment Content-Disposition with an ASCII fallback plus an
    RFC 5987 UTF-8 name, so non-ASCII (e.g. Chinese) skill names download with
    the correct filename across browsers."""
    ascii_name = filename.encode("ascii", "ignore").decode().strip() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@router.post("/{skill_id}/external-download", response_model=ExternalDownloadRead)
def record_external_download(
    skill_id: str,
    request: Request,
    eid: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ExternalDownloadRead:
    """记一次「用户去上游取这个包」,并把该去的地址交出来。**不传字节。**

    存在的理由是 `download_skill` 的 307 那条分支**在浏览器里走不通**:前端的下载按钮走的是
    XHR(`responseType: 'blob'`),而 XHR 跟着一个跨域跳转时浏览器会对新域名做 CORS 检查 ——
    Nexus 不回 `Access-Control-Allow-Origin`,请求在协议层就被掐掉,连状态码都没有。用户看到的
    是「Network error / Failed to fetch」,而服务端日志里明明白白写着 307。这不是配置问题:只要
    下载走 XHR,转到一个不给 CORS 头的外部主机就永远失败。

    所以界面改成一个真的链接(顶层导航不受 CORS 约束),而这一次「去取」的计数就落在这里。
    307 那条分支保留不动:它对 curl / CLI 这类会跟跳转的调用方是对的,只是浏览器用不了。

    **两条路不会重复计数**,因为前端对同一个技能只会走其中一条(按 `externally_hosted` 分岔)。
    """
    skill = _get_skill_or_404(db, skill_id)
    if not skill.externally_hosted:
        # 不是外部托管的技能没有「去上游取」这件事。拒掉而不是静默记一次:否则这个端点就是一个
        # 谁都能调、不用真的下载任何东西就能把下载量刷上去的入口。
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "这个技能的内容在门户自己这边,请用 /download 下载",
        )
    _bump_downloads(db, skill, acting_eid(request, eid))
    return ExternalDownloadRead(
        id=skill.id, url=skill.external_url, source=skill.external_source
    )


@router.get("/{skill_id}/download")
def download_skill(
    skill_id: str,
    request: Request,
    eid: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Download a skill as a .zip archive named after the skill's real name.

    Who downloaded is taken from the JWT the auth middleware verified, falling
    back to the ``eid`` query param only when auth is off in dev — the same rule
    the market's download endpoint already follows. It used to read the query
    param alone, which had two consequences: a caller that omitted it produced a
    row nobody could be attributed to (ten such rows exist, 2026-08-06 to
    2026-08-12), and a caller that supplied someone else's EID had its download
    recorded against them. Neither is acceptable for a figure the report presents
    as "who uses what".

    The archive contains the whole skill folder (SKILL.md + any artifacts +
    manifest) under a top-level directory named after the skill. Skills with
    no folder on disk fall back to a zip containing a single SKILL.md built
    from the stored instructions.
    """
    skill = _get_skill_or_404(db, skill_id)
    downloader = acting_eid(request, eid)

    if skill.externally_hosted:
        # 内容在别人那儿(第一个来源是 IVC 的 Nexus),门户没有字节可发。
        #
        # **先记事件,再转出去。** 顺序是这里唯一有意义的决定:转出去之后门户什么都看不到了,
        # 所以记在前面。代价是用户在对方那边失败了我们仍记了一次 —— 与市场那侧同一个取舍
        # (见 market.py:"记在传输之前,失败会抛"),理由也一样:"没人下得下来"正是这个计数
        # 应该让人看见的事,而不是应该抹掉的事。
        _bump_downloads(db, skill, downloader)
        # 307 而不是 302:保持方法与请求体不变,并且明确这不是一个永久搬迁 —— 防火墙放行之后
        # 这批技能会改成门户自己存包,那时这个分支对它们就不再成立。
        return RedirectResponse(skill.external_url, status_code=307)

    # Official (anthropic) skills: name the archive after the namespaced id
    # (e.g. "anthropics-xlsx"); user skills after the display name.
    is_official = skill.category_id == "official"
    safe_name = (
        sanitize_segment(skill.id, skill_id)
        if is_official
        else sanitize_segment(skill.name, skill_id)
    )
    if not skill.storage_path or not skill.repo_commit:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "这个技能没有记下内容在仓库里的位置或版本,无法下载",
        )
    try:
        tree = asset_content.read_tree(skill.storage_path, skill.repo_commit)
    except asset_content.ContentUnavailable as exc:
        # 原来这里会用 skill.instructions 合成一个只有 SKILL.md 的 zip。那是同一份正文的第二份
        # 副本,而且下载下来的包看不出它缺了脚本和附件 —— 用户会以为自己拿到了完整的技能。
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "暂时读不到技能内容,请稍后重试"
        ) from exc

    buf = io.BytesIO()
    wrote_any = False
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        seen: set[str] = set()

        def _add(rel: str, data: bytes) -> None:
            nonlocal wrote_any
            rel = rel.strip("/")
            if not rel:
                return
            arc = str(Path(safe_name) / rel)
            if arc in seen:
                return
            seen.add(arc)
            zf.writestr(arc, data)
            wrote_any = True

        # 1) A user's uploaded artifact archive is the source of truth for the
        #    skill's files: it holds the author's original folder (SKILL.md +
        #    scripts + references). Ship its contents, stripping the single
        #    top-level wrapper dir so the skill isn't double-nested under
        #    <name>/<name>/SKILL.md. The loose root SKILL.md is only the
        #    extracted definition; manifest.json / artifacts/ are marketplace-
        #    only and must not leak into the downloaded skill.
        artifact = _artifact_member(tree) if not is_official else None
        if artifact is not None:
            try:
                with zipfile.ZipFile(io.BytesIO(artifact)) as src:
                    members = [m for m in src.infolist() if not m.is_dir()]
                    top = _common_top_dir(members)
                    for m in members:
                        rel = m.filename[len(top) + 1 :] if top else m.filename
                        _add(rel, src.read(m))
            except zipfile.BadZipFile:
                # Corrupt / not-really-a-zip artifact: fall back to loose files.
                pass

        # 2) No usable artifact archive: ship the loose skill files, dropping
        #    the marketplace-only manifest.json and the artifacts/ directory.
        if not wrote_any:
            for rel in sorted(tree):
                parts = Path(rel).parts
                if (parts and parts[0] == "artifacts") or Path(rel).name == "manifest.json":
                    continue
                _add(rel, tree[rel])

        # 3) 仓库里那一版真的什么都没有:报错,不拿数据库里的副本凑一个包出来。
        if not wrote_any:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "该 Skill 没有可下载的内容"
            )

    buf.seek(0)
    _bump_downloads(db, skill, downloader)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(f"{safe_name}.zip")},
    )


@router.post("", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
def create_skill(payload: SkillCreate, db: Session = Depends(get_db)) -> RpAiSkill:
    skill_id = payload.id or _slugify(payload.name)
    if db.get(RpAiSkill, skill_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Skill '{skill_id}' already exists"
        )
    data = payload.model_dump(exclude={"id", "cases", "sub_skills", "input_params"})
    skill = RpAiSkill(id=skill_id, **data)
    _apply_children(skill, payload)
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def _refuse_if_synced(skill: RpAiSkill) -> None:
    """从上游同步来的技能在门户里是只读的。

    门户是 Gerrit 上 ``radio/ai-assets`` 的**下游**。在这里改或下架有两层问题:一层是徒劳 ——
    下一次同步会照上游的内容写回来;另一层是真会弄坏东西 —— 这两条路径都会拿
    ``storage_path`` 当"门户自己那个技能仓里的路径"去写,而同步来的值是 ``gerrit:skill/xxx``,
    于是 ``skill_storage`` 会在门户的仓里造一个名叫 ``gerrit:skill`` 的目录,或者试图去删一份
    那里根本不存在的内容。

    所以这里明确拒绝,并说清该去哪改。要真的下架一个同步来的技能,得先从上游那个仓里去掉。
    """
    if asset_content.is_gerrit(skill.storage_path):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "这个技能是从 Gerrit 的 radio/ai-assets 同步来的,门户只是它的只读下游 —— "
            "在这里的修改会被下一次同步覆盖回去。请改上游那个仓。",
        )


@router.put("/{skill_id}", response_model=SkillRead)
async def update_skill(
    skill_id: str, payload: SkillUpdate, request: Request, db: Session = Depends(get_db)
) -> RpAiSkill:
    # Who is editing is the signed-in caller, not a field in the body (see acting_eid).
    editor_eid = acting_eid(request, payload.editor_eid)
    skill = _get_skill_or_404(db, skill_id)
    _refuse_if_synced(skill)
    scalar_data = payload.model_dump(
        exclude_unset=True,
        exclude={"cases", "sub_skills", "input_params", "editor_eid", "skill_md"},
    )
    for key, val in scalar_data.items():
        setattr(skill, key, val)
    # author always stores the uploader's EID (identity), never a display name;
    # the human display name is resolved from the user record at read time.
    skill.author = skill.uploader_eid or editor_eid or skill.author
    # When the group (category) is changed, refresh the denormalized label.
    if "category_id" in scalar_data and skill.category_id:
        cat = db.get(RpAiCategory, skill.category_id)
        if cat:
            skill.category = cat.label
        # 部门默认图也要跟着换。原来这里只刷新了 ``category`` 标签,于是把技能挪到别的部门之后,
        # 卡片上还挂着**原部门**的图,而界面上没有任何地方能纠正它(封面图此前完全不可编辑)。
        #
        # 两个例外都不动:作者传过自己的封面图(那是他的选择,不该被换组悄悄覆盖),或者这次请求
        # 显式带了 image(调用方指定的优先)。
        if not skill.has_custom_image and "image" not in scalar_data:
            skill.image = skill_image.category_image(skill.category_id)
    _apply_children(skill, payload)

    # If the raw SKILL.md was edited directly, it is the source of truth:
    # write it verbatim and sync name/description from its frontmatter.
    if payload.skill_md is not None:
        content = payload.skill_md
        if not has_name_and_description(content):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "SKILL.md 必须包含 name: 和 description: 两个字段",
            )
        fm_name, fm_desc = parse_frontmatter(content)
        if fm_name:
            skill.name = fm_name
        if fm_desc is not None:
            skill.description = fm_desc
        # 同上:不再写 instructions。
        md_content = content
    else:
        # 只改了元数据:正文取仓库里现有的那一份,按引用读。
        #
        # 原来这里用 `render_skill_md(skill)`,它读 `skill.instructions`;那一列不再写之后它会
        # 走到"用 name/description 合成一个正文"的回落上 —— 于是这次审计审的是一份合成出来的东西,
        # 而不是仓库里真正存着的内容,审计结论也就不对应任何真实内容。
        md_content = _read_skill_md(skill)
        if not has_name_and_description(md_content):
            # 仓库里那份没有 frontmatter(老内容):补上当前的 name/description 再送审,
            # 与上传路径的处理一致。
            md_content = with_frontmatter(md_content, skill.name, skill.description)

    db.flush()  # make edited fields/children visible for manifest building

    # The same audit as an upload, on the same content, before anything is
    # written. This route used to write straight to the skill repo: the audit was
    # attached to the upload route instead of to the act of writing, so editing a
    # skill was a way to put unaudited content in the repo.
    editor = editor_eid or skill.uploader_eid
    audit: AuditResult = await run_in_threadpool(
        lambda: audit_skill(
            name=skill.name,
            skill_md=md_content,
            uploader_eid=editor,
        )
    )
    decision = gate.decide(audit=audit, skill_id=skill.id,
                           replaces_published=skill.status == "approved")

    if decision.unavailable:
        db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "安全审计暂时不可用，本次编辑未保存，请稍后重试。",
        )

    if decision.blocked:
        # Nothing is written and the edit itself is discarded, but the record of
        # the rejection is kept: "why was this refused" has to be answerable.
        #
        # The record is deliberately not linked to the skill. The projection takes
        # the skill's displayed verdict from its latest linked record, so linking a
        # rejected *edit* would mark a live skill as dangerous while what is
        # published is still the older content that passed. A rejected edit says
        # nothing about the published content; the skill name keeps it traceable.
        db.rollback()
        gate.record(db, decision.ref, audit, skill_id=None,
                    skill_name=skill.name, uploader_eid=editor)
        db.commit()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"编辑被安全审计拒绝：{audit.reason or '命中安全红线'}。参考编号 {decision.ref}",
        )

    # 这次改动的内容(改完之后的样子),在技能行被回滚之前抄下来。只有要走人工复核、且这个技能
    # 已经发布过的时候才用得上;抄一份的代价是几个字段的读,所以不分支。
    staged = pending_updates.capture(skill)
    staged_name = skill.name

    # Persist the edit into the git-managed skill repo: refresh the folder
    # files, bump the version, commit, tag and push (same flow as upload).
    owner_eid = skill.uploader_eid or "ericsson"
    slug = sanitize_segment(skill.category_id or "uncategorized", "uncategorized")
    result = skill_storage.store(
        skill_id=skill.id,
        eid=owner_eid,
        name=skill.name,
        category_slug=skill.category_id or "uncategorized",
        manifest=build_manifest(skill, owner_eid, slug),
        instructions=md_content,
        artifact_bytes=None,
        artifact_filename=None,
        current_version=skill.version,
        existing_storage_path=skill.storage_path,
        audit=audit,
        action="edit",
        target_branch=decision.review_branch,
    )

    if decision.stays_published:
        # 要复核、而且这个技能已经在线上:技能行一个字不动 —— 名称、版本、``repo_commit``、状态
        # 全部继续指向已发布的那一版,它照常留在市场里。这次更新整份停在暂存表,等复核 MR 合了
        # 再搬过去(见 skill_audit.pending)。
        #
        # 回滚是为了把改动从技能行上撤下来:改动是先落在 ORM 对象上再送审的(审的就是它渲染出来
        # 的内容),不撤就会随下面的 commit 一起写进去。审计记录还没落库,所以回滚不丢事实。
        db.rollback()
        skill = _get_skill_or_404(db, skill_id)
    else:
        skill.version = result.version
        skill.storage_path = result.storage_path
        skill.repo_commit = result.commit_sha
        # 没有已发布版本可以保护的时候,老规矩:通过即上线,可疑则等复核 MR 合。
        skill.status = decision.status
        skill.audit_state = audit.state
        # 哪条记录描述这一版(见 models/skill.py)。
        skill.audit_ref = decision.ref

        db.add(
            RpAiSkillVersion(
                skill_id=skill.id,
                version=result.version,
                commit_sha=result.commit_sha,
                tag=result.tag,
                uploader_eid=editor_eid or owner_eid,
                notes="Edited via portal",
            )
        )
        # 收藏者收到的"更新到 vX"必须对应一个装得到的版本,所以这条通知只在内容真的发布时发。
        notify_version_update(db, skill, result.version, actor_eid=editor_eid)

    # A review is a person reading a change, so the change has to be somewhere
    # they can read it: same Draft MR as an upload takes.
    mr_url = None
    if decision.needs_review:
        try:
            mr_url = await run_in_threadpool(
                lambda: gitlab_mr.review_suspect(
                    source_branch=decision.review_branch,
                    target_branch=settings.skill_repo_branch,
                    skill_name=staged_name,
                    findings=audit.findings,
                    audit_ref=decision.ref,
                    uploader=gate._uploader_label(db, editor),
                    reason=audit.reason,
                    coverage=audit.coverage,
                    cause=audit.cause,
                )
            )
        except gitlab_mr.ReviewRequestFailed as exc:
            # 复核 MR 开不出来就当这次发布没成功:把刚推的复核分支收回去,让请求失败。
            # 否则内容留在分支上、数据库那一行是 pending,而没有人被通知、也没有东西可复核 ——
            # 上传者看到的"等待复核"是假的。数据库还没提交,所以这里抛出去不会留下半个状态。
            skill_content.delete_branch(decision.review_branch)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "内容需要人工复核,但复核单没能创建,本次未保存,请稍后重试。",
            ) from exc

    if decision.stays_published:
        pending_updates.stage(
            db, skill_id=skill.id, staged=staged, audit_ref=decision.ref,
            review_branch=decision.review_branch, mr_url=mr_url,
            version=result.version, storage_path=result.storage_path,
            commit_sha=result.commit_sha, tag=result.tag,
            artifact_name=result.artifact_name, editor_eid=editor_eid or owner_eid,
        )
    elif skill.pending_update is not None:
        # 这次改动直接发布了,它就是线上那一版;之前那份待复核的更新已经不是"线上之外压着的下一
        # 版"了,留着会在复核合并时把作者后来改的东西盖回去。
        pending_updates.drop(db, skill.pending_update, why="被一次直接发布的编辑取代")

    gate.record(db, decision.ref, audit, skill_id=skill.id,
                skill_name=staged_name, uploader_eid=editor, mr_url=mr_url,
                replaces_published=decision.replaces_published)

    db.commit()
    db.refresh(skill)

    # 写成功即内容在 GitLab 上:没有本地提交,也就没有"已保存但没同步"这种状态。写不成会抛,
    # 由 main.py 的处理器答成 409/503,数据库那一行还没写(顺序是先仓库后数据库)。
    # `synced` 字段保留(前端在读),但它现在恒为真;原来那条 sync_warning 分支不可达。
    skill.synced = result.pushed
    return skill


# --- 封面图 -----------------------------------------------------------------
#
# 这三个端点**刻意不复用 update_skill**。那条路每次都会重跑一次安全审计、升版本、往 GitLab
# 提交打 tag、写一行版本历史、并通知所有收藏者"已更新到 vX";而且审计判 suspect 时整份改动会被
# 暂存进待复核表,于是图片改动根本不生效。为换一张封面付这些代价是错的:封面是展示元数据,不是
# 技能内容,它变了没有任何人需要重新安装。
#
# 所以这里只写那三列,不碰 version / repo_commit / status / audit_* ,也不发通知。


def _require_image_owner(
    skill: RpAiSkill, request: Request, x_user_eid: str | None
) -> str:
    """只有上传者本人和管理员能改封面图。返回调用者 EID。

    照 delete_skill / market 那边的规矩:身份取已验证的调用者,不是调用方能随便填的请求头。
    ``uploader_eid`` 为空时**不放行** —— 官方导入的技能就是没有上传者的,放行等于任何登录用户
    都能改它们的封面。
    """
    requester = acting_eid(request, x_user_eid)
    if requester == settings.audit_admin_eid:
        return requester
    if requester != skill.uploader_eid:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "只有上传者本人或管理员可以修改封面图"
            if skill.uploader_eid
            else "这个技能没有登记上传者,只有管理员可以修改封面图",
        )
    return requester


@router.put("/{skill_id}/image", response_model=SkillImageState)
async def set_skill_image(
    skill_id: str,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> RpAiSkill:
    """换掉这个技能的封面图。PUT 而非 POST:同一张图重传两次结果相同。"""
    skill = _get_skill_or_404(db, skill_id)
    _require_image_owner(skill, request, x_user_eid)

    # 按块读并累计,不用 `await file.read()` 一口吞下整个 body。注意这拦的是进程内存:
    # python-multipart 在进到这个函数之前已经把请求体落到临时文件了,真正的传输上限属于边缘
    # (nginx client_max_body_size)。所以这道闸门的意义是"库里不会出现超限的行",不是省带宽。
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > skill_image.MAX_UPLOAD_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"图片不能超过 {skill_image.MAX_UPLOAD_BYTES // 1024} KB",
            )
        chunks.append(chunk)

    # 解码 + 重新编码是 CPU 活,别占着事件循环(一张 4000px 的图能跑上百毫秒)。
    try:
        data, mime = await run_in_threadpool(
            lambda: skill_image.normalize(b"".join(chunks))
        )
    except skill_image.InvalidImage as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    skill.image_blob = data
    skill.image_mime = mime
    # 用库的时钟,不用 datetime.utcnow():这一列和 created_at/updated_at 一样存 DB 本地的 naive
    # 值(那台库是 UTC+2),混用两个时钟正是 audit.py 里记着的那个坑。utcnow() 另外在 3.12 起已弃用。
    skill.image_updated_at = reporting_windows.db_now()
    db.commit()
    db.refresh(skill)
    return skill


def _image_etag(skill: RpAiSkill) -> str:
    """强 ETag。图变了 ``image_updated_at`` 一定变,所以它足够做版本标识。"""
    stamp = skill.image_updated_at.isoformat() if skill.image_updated_at else "0"
    return '"' + hashlib.sha256(f"{skill.id}:{stamp}".encode()).hexdigest()[:32] + '"'


@router.get("/{skill_id}/image")
def get_skill_image(
    skill_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    """返回作者上传的封面图。没有自定义图就是 404 —— 前端据此回落到部门默认图。

    这里**不做**内容协商也不做缩略图:存进来的时候已经归一化过(≤1024px、无 EXIF、格式限定在
    PNG/JPEG/WebP),所以直接吐字节。
    """
    skill = _get_skill_or_404(db, skill_id)
    if not skill.has_custom_image:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "这个技能没有自定义封面图"
        )

    etag = _image_etag(skill)
    headers = {
        "ETag": etag,
        # 一分钟的陈旧度换掉市场页几十张图的重复请求;配合 ETag,换图之后最多一分钟就刷新,
        # 而且刷新是一次 304 不是一次重传。
        "Cache-Control": "private, max-age=60, must-revalidate",
        # 这是用户提供过的内容,即使已经重新编码过也不给浏览器自己猜类型的机会。
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    # image_blob 是 deferred 的,这一句才真的把字节读出来 —— 也只有这条路径需要它。
    return Response(content=skill.image_blob, media_type=skill.image_mime, headers=headers)


@router.delete("/{skill_id}/image", response_model=SkillImageState)
def clear_skill_image(
    skill_id: str,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> RpAiSkill:
    """删掉自定义封面图,回落到部门默认图。

    顺手把 ``image`` 重新按当前部门派一次:这一行原本可能还留着旧部门的图(见 update_skill
    里那处修复),而作者点"移除封面图"时期望看到的是**现在这个部门**该有的那张。
    """
    skill = _get_skill_or_404(db, skill_id)
    _require_image_owner(skill, request, x_user_eid)
    skill.image_blob = None
    skill.image_mime = None
    skill.image_updated_at = None
    skill.image = skill_image.category_image(skill.category_id or "")
    db.commit()
    db.refresh(skill)
    return skill


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(
    skill_id: str,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> None:
    """下架一个技能:把内容从仓库移掉,数据库那一行置成 archived。

    **为什么不删那一行。** 它带着仓库里算不出来的门户事实:下载量、收藏、评价、审计记录。删掉它等于
    为了跟仓库保持一致而销毁门户自己的记录。公开列表按 ``status == "approved"`` 过滤,所以置成
    ``archived`` 之后它自然从市场消失,前端不用改。

    **为什么要动仓库。** 只删数据库那一行的话,manifest 还在仓库里;收敛器补齐 manifest 那一步一上线,
    这个技能会被重新扫回来。内容的权威在仓库,所以"删除"必须先在仓库里发生。

    鉴权照 market 那边的规矩:身份取已验证的调用者(不是调用方能改的请求头),只有上传者本人和管理员
    可以下架。原来这个接口**没有任何鉴权**,任何人可以删任何技能。
    """
    skill = _get_skill_or_404(db, skill_id)
    _refuse_if_synced(skill)
    requester = acting_eid(request, x_user_eid)
    is_admin = requester == settings.audit_admin_eid
    # `uploader_eid` 为空时**不放行** —— 官方导入的技能就是没有上传者的,而原来的写法
    # (`skill.uploader_eid and ...`)让任何登录用户都能把它们下架。
    if not is_admin and requester != skill.uploader_eid:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "只有上传者本人或管理员可以下架这个技能"
            if skill.uploader_eid
            else "这个技能没有登记上传者,只有管理员可以下架",
        )

    if skill.storage_path and skill.repo_commit:
        try:
            skill_content.publish(
                prefix=skill.storage_path,
                files={},
                expect=skill.repo_commit,
                branch=settings.skill_repo_branch,
                message=f"remove: {skill.name} (下架, 操作者 {requester})",
            )
        except skill_content.NotFound:
            pass  # 仓库里已经没有了,这次下架只需要改状态
        except skill_content.Conflict:
            # 内容在我们记下的那一版之后又动过。`publish` 是按文件带 `last_commit_id` 做前置
            # 条件的,所以**上一次下架自己就会造成这个冲突**:它把文件删掉、把分支推过了库里记
            # 着的 `repo_commit`,而那一行的 `repo_commit` 从来不更新。于是第二次下架永远撞
            # 409,界面上再也删不掉 —— 2026-09-09 线上就卡在这个状态(第一次 204、第二次 409)。
            #
            # 下架只关心一件事:这份内容现在还在不在分支上。
            #   不在 -> 这一步要达成的目标已经达成,继续去改状态;
            #   还在 -> 是真的有人并发改了同一个技能,照旧把 409 抛给调用方。
            # 这样保住了 `expect` 那道保护,又让重试能收敛。不能改成不带 `expect`:那是静默覆盖。
            if _content_present_on_branch(skill.storage_path):
                raise
            logger.info(
                "下架 %s:内容已不在 %s 分支上,跳过仓库这一步",
                skill.id,
                settings.skill_repo_branch,
            )
    skill.status = "archived"
    db.commit()
