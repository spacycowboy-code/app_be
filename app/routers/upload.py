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
from app.services.audit_report import build_checks
from app.services import gitlab_mr
from fastapi.concurrency import run_in_threadpool

router = APIRouter(prefix="/api/skills", tags=["upload"])
settings = get_settings()


def _category_image(category_id: str) -> str:
    """Frontend image asset for a category group."""
    # The 'public' group has no dedicated image; reuse tavily art.
    slug = category_id or "product-system"
    if slug == "public":
        return "skill-images/tavily.jpg"
    # AI team group has no dedicated art; use the shared default image.
    if slug == "ai-team":
        return "skill-images/default.png"
    return f"skill-images/{slug}.jpg"


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
    if db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid)) is None:
        db.add(RpAiPortal(eid=eid))
        db.flush()


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
    # Auto-assign the image for this skill's group.
    skill.image = _category_image(category_id)
    if parsed_tags:
        skill.tags = parsed_tags
    if definition is not None:
        skill.instructions = definition
    skill.version = result.version
    skill.storage_path = result.storage_path
    skill.repo_commit = result.commit_sha
    # One lifecycle field gates visibility; the audit verdict decides its value.
    # A pass goes live immediately, a suspect waits for the review MR to be
    # merged, and a danger verdict never reaches this point (nothing is stored).
    # Recomputable from the audit record + MR state - see skill_audit.reconcile,
    # which is the only other writer.
    skill.status = decision.status
    # Latest verdict for the current version: display only, never a gate.
    skill.audit_state = audit.state

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

    # Suspect -> open a Draft MR for review (best-effort); then record the audit.
    mr_url = None
    if suspect:
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

_register_stream(router, _read_upload, _checks_out, _findings_out, acting_eid)
