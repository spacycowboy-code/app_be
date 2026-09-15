"""Skill upload endpoint.

Accepts a multipart upload (metadata + optional artifact file), stores the
skill into the git-managed skill repo (bumping its version), and records the
result in the database.
"""
import json
import re
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.category import RpAiCategory
from app.services.dept_groups import PSEUDO_CATEGORY_IDS, group_for_eid
from app.services import rdg_tags
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.models.version import RpAiSkillVersion
from app.schemas.version import SkillUploadResult, SkillVersionRead
from app.schemas.audit import SkillEvaluateResult, AuditFinding, AuditCheck
from app.services.skill_manifest import (
    extract_skill_md_from_zip,
    has_name_and_description,
    frontmatter_problem,
    parse_frontmatter,
)
from app.services.skill_storage import skill_storage
from app.services.notify import notify_version_update
from app.services.eids import acting_eid, normalize_eid
from app.services.audit_engine import AuditResult, audit_skill, content_key
from app.models.audit import RpAiSkillAuditRecord
from app.services.skill_audit import gate
from app.services.skill_audit import pending as pending_updates
from app.services.audit_report import build_checks
from app.repositories import skill_content
from app.services import gitlab_mr
from app.services import skill_image
from fastapi.concurrency import run_in_threadpool

router = APIRouter(prefix="/api/skills", tags=["upload"])
settings = get_settings()


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or f"skill-{uuid.uuid4().hex[:8]}"


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(t).strip() for t in val if str(t).strip()]
    except json.JSONDecodeError:
        pass
    return [t.strip() for t in raw.split(",") if t.strip()]


def _ensure_user(db: Session, eid: str) -> None:
    """建行时一并写上 department（技能上传）。

    这里原本是 ``RpAiPortal(eid=eid)`` 只写工号。报告与 Department Details 都按
    department 归属，列是空的就等于这个人不属于任何部门、名下资产落进「无法归属」——
    而这不报错：行建出来了、动作成功了、只是那个人从部门统计里消失了。
    2026-09-07 在 ``ivc_sync`` 修过同一个形状，代价是 5 个人 10 件技能。
    """
    ensure_portal_user(db, eid)


async def _read_upload(
    file: UploadFile | None, instructions: str | None
) -> tuple[str, bytes | None, str | None]:
    """Read + validate the uploaded skill definition (shared by evaluate/publish).

    Returns (definition_text, artifact_bytes, artifact_filename). The definition
    is the SKILL.md text (from the file, a SKILL.md inside a zip, or the
    instructions fallback) and MUST declare name: and description:.
    """
    artifact_bytes: bytes | None = None
    artifact_filename: str | None = None
    file_text: str | None = None
    if file is not None:
        artifact_bytes = await file.read()
        if len(artifact_bytes) > settings.max_upload_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"File exceeds {settings.max_upload_bytes} bytes",
            )
        artifact_filename = file.filename
        try:
            file_text = artifact_bytes.decode("utf-8")
        except UnicodeDecodeError:
            file_text = None
        # Binary uploads (e.g. a .zip package) can't be decoded as text.
        if file_text is None:
            file_text = extract_skill_md_from_zip(artifact_bytes)

    definition = file_text if file_text is not None else instructions
    if not definition or not has_name_and_description(definition):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Skill 定义必须包含 name: 和 description: 两个字段",
        )
    problem = frontmatter_problem(definition)
    if problem:
        # The check above only looks for the two labels. This one asks whether the block is
        # actually YAML, because that is what every downstream reader assumes — including the
        # scanner, which refuses to load the skill at all when it is not, so the audit ends as
        # "not checked" and a reviewer is handed a skill nobody looked at. Saying it here costs
        # the author one edit instead of an audit, a review MR and a second identical upload.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
    return definition, artifact_bytes, artifact_filename


async def _audit_reusing_record(
    db: Session, *, name: str, definition: str, artifact_bytes: bytes | None,
    artifact_filename: str | None, eid: str,
) -> AuditResult:
    """Audit this content, reusing a fresh verdict for identical bytes if one exists.

    evaluate and publish audit the same bytes moments apart and a scan costs tens of
    seconds. The audit record is the shared fact, so the reuse goes through it rather than
    through process memory — with more than one API replica the two calls can land on
    different ones, and a process-local cache would silently pay twice.
    """
    key = content_key(name, definition, artifact_bytes)
    cached = gate.recent_audit(db, key)
    if cached is not None:
        return cached
    return await run_in_threadpool(
        lambda: audit_skill(
            name=name,
            skill_md=definition,
            artifact_bytes=artifact_bytes,
            artifact_filename=artifact_filename,
            uploader_eid=eid or None,
        )
    )


def _checks_out(audit: AuditResult) -> list[AuditCheck]:
    """The report the wizard renders: fixed checks, evidence grouped under them."""
    return [
        AuditCheck(
            id=c.id, label=c.label, status=c.status, description=c.description,
            severity=c.severity, note=c.note,
            findings=[_finding_out(f) for f in c.findings],
        )
        for c in build_checks(audit)
    ]


def _finding_out(f) -> AuditFinding:
    """Serialize one diagnostic, including where it is and what to change."""
    return AuditFinding(
        source=f.source, category=f.category, severity=f.severity,
        evidence=f.evidence, detail=f.detail,
        file=f.file, line=f.line, remediation=f.remediation,
        # The scanner's rule id and taxonomy code travel with the finding: without them
        # a reader cannot look up what fired, and the report would only have our wording.
        rule_id=f.rule_id or None, aitech=f.aitech,
    )


def _findings_out(audit: AuditResult) -> list[AuditFinding]:
    """Serialize findings for the API (secret values already redacted upstream)."""
    return [_finding_out(f) for f in audit.findings]


@router.post("/evaluate", response_model=SkillEvaluateResult)
async def evaluate_skill(
    request: Request,
    db: Session = Depends(get_db),
    eid: str = Form("", description="Uploader EID (ignored when signed in)"),
    name: str = Form(...),
    instructions: str | None = Form(None),
    file: UploadFile | None = File(None),
) -> SkillEvaluateResult:
    """Audit-only preview: run the audit, store NOTHING, return the verdict.

    The upload wizard calls this at its 'evaluating' step so the result screen
    shows the real state + findings (no score) before the user commits. publish
    re-runs the same audit as the authoritative, non-bypassable gate.
    """
    eid = acting_eid(request, eid)
    definition, artifact_bytes, artifact_filename = await _read_upload(file, instructions)
    audit: AuditResult = await _audit_reusing_record(
        db, name=name, definition=definition, artifact_bytes=artifact_bytes,
        artifact_filename=artifact_filename, eid=eid,
    )
    reference = uuid.uuid4().hex
    # Danger is blocked in the UI (no publish button) so it never reaches
    # publish; persist the blocked-attempt record here for traceability.
    if audit.is_danger:
        gate.record(
            db, reference, audit, skill_id=None, skill_name=name, uploader_eid=eid or None
        )
        db.commit()
    return SkillEvaluateResult(
        state=audit.state, reference=reference, reason=audit.reason,
        coverage=audit.coverage, cause=audit.cause,
        checks=_checks_out(audit), findings=_findings_out(audit),
    )


@router.post(
    "/upload", response_model=SkillUploadResult, status_code=status.HTTP_201_CREATED
)
async def upload_skill(
    request: Request,
    db: Session = Depends(get_db),
    eid: str = Form(..., description="Uploader EID (ignored when signed in)"),
    name: str = Form(...),
    category_id: str = Form(..., alias="categoryId"),
    description: str | None = Form(None),
    author: str | None = Form(None, description="Display name; defaults to owner EID"),
    tags: str | None = Form(None, description="JSON array or comma-separated"),
    rdg_tools: str | None = Form(
        None,
        alias="rdgTools",
        description="R&D Guide tool names this skill belongs to (JSON array or comma-separated)",
    ),
    instructions: str | None = Form(None),
    skill_id: str | None = Form(None, alias="id"),
    file: UploadFile | None = File(None),
) -> SkillUploadResult:
    eid = acting_eid(request, eid)
    if not eid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "eid is required")

    definition, artifact_bytes, artifact_filename = await _read_upload(file, instructions)

    # The card/description shown in the market comes from the SKILL.md
    # `description:` frontmatter; the (optional) form field only overrides it.
    _, fm_description = parse_frontmatter(definition)
    effective_description = description if description is not None else fm_description

    # --- Security audit: runs BEFORE anything is stored -------------------
    # danger  -> reject, never stored (only the audit record is kept).
    # suspect -> stored on a review branch + Draft MR (hidden from browse).
    # pass    -> stored live as usual.
    audit: AuditResult = await _audit_reusing_record(
        db, name=name, definition=definition, artifact_bytes=artifact_bytes,
        artifact_filename=artifact_filename, eid=eid,
    )
    audit_ref = uuid.uuid4().hex
    if audit.is_danger:
        gate.record(
            db, audit_ref, audit, skill_id=None, skill_name=name, uploader_eid=eid
        )
        db.commit()
        # The reason is the only actionable part: without it the author can only
        # re-upload the same bytes and be refused again.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"上传被安全审计拒绝：{audit.reason or '命中安全红线'}。参考编号 {audit_ref}",
        )

    # Resolve identity: existing skill (by id) -> re-upload/version bump.
    existing: RpAiSkill | None = None
    if skill_id:
        existing = db.get(RpAiSkill, skill_id)
    resolved_id = skill_id or _slugify(name)
    if existing is None and db.get(RpAiSkill, resolved_id) is not None:
        # New upload but slug collides with another skill; make it unique.
        resolved_id = f"{resolved_id}-{uuid.uuid4().hex[:6]}"

    _ensure_user(db, eid)

    # Category: category_id is the slug. Two things were missing here — an
    # unknown slug was written through to the row, the manifest and the git path
    # (a skill filed under a category nobody filters by is invisible everywhere
    # except "All"), and an empty one had no fallback even though the uploader's
    # department already implies a group.
    category_id = (category_id or "").strip()
    if not category_id:
        category_id = group_for_eid(db, eid) or ""
    if not category_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Choose a group for this skill: your department does not map to one.",
        )
    if category_id in PSEUDO_CATEGORY_IDS:
        # 'all' is a filter, not a home. Storing it would hide the skill from
        # every real group while looking like a valid choice.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"'{category_id}' is not a group you can upload to."
        )
    category = db.get(RpAiCategory, category_id)
    if category is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown group '{category_id}'."
        )
    category_label = category.label

    # R&D Guide 关联:**在动任何东西之前**校验。这个端点后面还要跑安全审计、往仓库推文件、
    # 建复核 MR —— 一个名字打错的请求不该先产生这些副作用再被拒。
    parsed_rdg_tools = _parse_tags(rdg_tools)
    rdg_rejection = rdg_tags.rejection_for(db, parsed_rdg_tools, category_id)
    if rdg_rejection:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, rdg_rejection)

    # Build manifest metadata (persisted alongside the files).
    parsed_tags = _parse_tags(tags)
    manifest = {
        "name": name,
        "description": effective_description,
        "author": existing.uploader_eid if existing else eid,
        "category": category_label,
        "categoryId": category_id,
        "tags": parsed_tags,
    }

    # The uploader on record is the original creator; keep it stable.
    owner_eid = existing.uploader_eid if existing else eid

    # Suspect skills are committed to a dedicated review branch, not live.
    decision = gate.decide(
        audit=audit, skill_id=resolved_id, ref=audit_ref,
        # A new version of a skill that is already published is subject to the
        # same rule: an audit that could not run must not unpublish it.
        replaces_published=bool(existing and existing.status == "approved"),
    )
    if decision.unavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "安全审计暂时不可用，本次上传未保存，请稍后重试。",
        )
    suspect = decision.needs_review
    review_branch = decision.review_branch

    # 审计记录在写内容**之前**落库并提交。它是数据库自己拥有的事实("审计跑过、结论是什么"),
    # 不是内容的投影,所以"先仓库后数据库"那条规矩不管它。原来它排在写内容之后,于是写失败时整条
    # 丢掉:跑了几十秒的审计没有痕迹,健康端点的失败率也少算一次。被拒的内容那条路本来就是先记后提交。
    # mr_url 这时还不知道(复核 MR 要等内容写完才开),等开完再补到这条记录上。
    # `skill_id` 是指向技能表的外键,而新技能这时候还没有行,所以只有已存在的技能才填它 ——
    # 被拒的内容那条路本来也是用 skill_id=None 记的。新技能的这一格等行建好之后补。
    gate.record(
        db, audit_ref, audit,
        skill_id=resolved_id if existing else None,
        skill_name=name, uploader_eid=eid,
        replaces_published=decision.replaces_published,
    )
    db.commit()

    result = skill_storage.store(
        skill_id=resolved_id,
        eid=owner_eid or eid,
        name=name,
        category_slug=category_id,
        manifest=manifest,
        instructions=definition,
        artifact_bytes=artifact_bytes,
        artifact_filename=artifact_filename,
        current_version=existing.version if existing else None,
        existing_storage_path=existing.storage_path if existing else None,
        audit=audit,
        target_branch=review_branch,
    )

    # Upsert the skill row.
    staged: dict = {}
    skill = existing
    if skill is None:
        skill = RpAiSkill(id=resolved_id, uploader_eid=eid, tags=[])
        db.add(skill)
    skill.name = name
    if effective_description is not None:
        skill.description = effective_description
    # author stores the uploader's EID (identity). The human display name is
    # resolved from the user record (uploader.display_name) at read time.
    skill.author = owner_eid or eid
    skill.category = category_label
    skill.category_id = category_id
    # Auto-assign the image for this skill's group. 作者上传后可以用
    # PUT /api/skills/{id}/image 换成自己的图;那条路只写 image_blob,不动这一列,所以
    # 删掉自定义图之后还能回落到这里派的这一张。
    skill.image = skill_image.category_image(category_id)
    if parsed_tags:
        skill.tags = parsed_tags
    # 不再写 instructions 这一列:正文的权威在仓库,详情接口按引用读。留着写会长出第二份会漂的
    # 副本,而且没有任何重算路径。列本身等一次迁移再删。

    if decision.stays_published:
        # 一个**已经在线上**的技能,这次传的新版本要走人工复核:技能那一行一个字不动,线上那一版
        # 照常留在市场里。这次的新版本整份停在暂存表,等复核 MR 合了再搬过去。
        #
        # 原来这里照常改行、把 status 打成 pending,于是作者传一个可疑的新版本,等于替所有使用者
        # 把这个技能下架 —— 而且详情页会按新的 repo_commit 读到复核分支上那份没过审的正文。
        staged = pending_updates.capture(skill)
        db.rollback()                       # 把改动从技能行上撤下来(审计记录已单独提交)
        skill = db.get(RpAiSkill, resolved_id)
    else:
        skill.version = result.version
        skill.storage_path = result.storage_path
        skill.repo_commit = result.commit_sha
        # R&D Guide 关联跟着这一版一起落。**只在直接发布这条路上写** —— 上面那条路
        # (新版本要人工复核)把技能行整个 rollback 了,线上那一版一个字不动,所以这次表单里的
        # 关联也不该生效:否则一份还没过审的上传就能改掉线上资产在 R&D Guide 上的位置。
        # 那条路复核合并时会把暂存的内容搬过去,关联要不要一起搬是那条路自己的问题,今天
        # 不在这个功能的范围里(暂存表里没有这个字段,所以行为是「保持原样」而不是「丢掉」)。
        if parsed_rdg_tools or rdg_tags.tags_for(db, "skill", resolved_id):
            # 空清单也要写,那是「取消全部关联」。加个条件只为省掉常见情况下的两次查询。
            rdg_tags.set_tags(db, "skill", resolved_id, parsed_rdg_tools)
        # One lifecycle field gates visibility; the audit verdict decides its value.
        # A pass goes live immediately, a suspect waits for the review MR to be
        # merged, and a danger verdict never reaches this point (nothing is stored).
        # Recomputable from the audit record + MR state - see skill_audit.reconcile,
        # which is the only other writer.
        skill.status = decision.status
        # Latest verdict for the current version: display only, never a gate.
        skill.audit_state = audit.state
        # 哪条记录描述这一版:收敛器靠它重算上面那一格,而不靠时间戳排序(见 models/skill.py)。
        skill.audit_ref = audit_ref

        # The version row and the audit record both reference this skill by foreign
        # key, and neither is linked to it by an ORM relationship, so the ordering of
        # the flush is not guaranteed. Insert the skill first: MySQL enforces the
        # constraint (sqlite does not, which is why this only shows up on a
        # production-like engine).
        db.flush()

        # Record the version.
        db.add(
            RpAiSkillVersion(
                skill_id=resolved_id,
                version=result.version,
                commit_sha=result.commit_sha,
                tag=result.tag,
                uploader_eid=eid,
                artifact_name=result.artifact_name,
            )
        )

        # Notify users who favorited this skill about the new version.
        if existing is not None:
            notify_version_update(db, skill, result.version, actor_eid=eid)

    # 可疑内容要开一张 Draft MR 供人复核。**不是 best-effort**:开不出来就当这次发布没成功。
    mr_url = None
    if suspect:
        try:
            mr_url = await run_in_threadpool(
                lambda: gitlab_mr.review_suspect(
                    source_branch=review_branch,
                    target_branch=settings.skill_repo_branch,
                    skill_name=name,
                    findings=audit.findings,
                    audit_ref=audit_ref,
                    uploader=gate._uploader_label(db, eid),
                    reason=audit.reason,
                    coverage=audit.coverage,
                    cause=audit.cause,
                )
            )
        except gitlab_mr.ReviewRequestFailed as exc:
            # 复核 MR 开不出来就当这次发布没成功:把刚推的复核分支收回去,让请求失败。
            # 否则内容留在分支上、数据库那一行是 pending,而没有人被通知、也没有东西可复核 ——
            # 上传者看到的"等待复核"是假的。数据库还没提交,所以这里抛出去不会留下半个状态。
            skill_content.delete_branch(review_branch)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "内容需要人工复核,但复核单没能创建,本次未保存,请稍后重试。",
            ) from exc
    # 记录已经在写内容之前落库了。这里补上写完才知道的两格:技能那一行的 id(新技能才需要)、
    # 以及复核 MR 的地址。
    filled = {"skill_id": resolved_id}
    if mr_url:
        filled["mr_url"] = mr_url
    db.execute(
        update(RpAiSkillAuditRecord)
        .where(RpAiSkillAuditRecord.id == audit_ref)
        .values(**filled)
    )

    if decision.stays_published:
        pending_updates.stage(
            db, skill_id=resolved_id, staged=staged, audit_ref=audit_ref,
            review_branch=review_branch, mr_url=mr_url,
            version=result.version, storage_path=result.storage_path,
            commit_sha=result.commit_sha, tag=result.tag,
            artifact_name=result.artifact_name, editor_eid=eid,
        )
    elif skill.pending_update is not None:
        # 这一版直接发布了,它就是线上那一版;之前那份待复核的更新不再是"下一版",留着会在复核
        # 合并时把这次的内容盖回去。
        pending_updates.drop(db, skill.pending_update, why="被一次直接发布的上传取代")

    db.commit()
    db.refresh(skill)

    return SkillUploadResult(
        skill=skill,
        version=result.version,
        commit_sha=result.commit_sha,
        tag=result.tag,
        storage_path=result.storage_path,
        pushed=result.pushed,
        audit_state=audit.state,
        reason=audit.reason,
        coverage=audit.coverage,
        cause=audit.cause,
        mr_url=mr_url,
        reference=audit_ref,
        checks=_checks_out(audit),
        findings=_findings_out(audit),
    )


@router.get("/{skill_id}/versions", response_model=list[SkillVersionRead])
def list_versions(skill_id: str, db: Session = Depends(get_db)) -> list[RpAiSkillVersion]:
    if db.get(RpAiSkill, skill_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Skill '{skill_id}' not found")
    return list(
        db.scalars(
            select(RpAiSkillVersion)
            .where(RpAiSkillVersion.skill_id == skill_id)
            .order_by(RpAiSkillVersion.id.desc())
        ).all()
    )


# Streaming twin of /evaluate. Registered here so it can reuse the helpers above rather
# than duplicating the upload parsing and the report shaping.
from app.routers.upload_stream import register as _register_stream  # noqa: E402
from app.services.portal_users import ensure_portal_user

_register_stream(router, _read_upload, _checks_out, _findings_out, acting_eid)
