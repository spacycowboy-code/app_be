"""Skill endpoints: browse, detail, create, update, delete."""
import io
import re
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
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
from app.schemas.skill import (
    SkillCreate,
    SkillRead,
    SkillSummary,
    SkillUpdate,
)
from app.services.skill_manifest import (
    build_manifest,
    has_name_and_description,
    parse_frontmatter,
    render_skill_md,
)
from app.services.skill_storage import sanitize_segment, skill_storage
from app.services import gitlab_mr
from app.services.skill_audit import audit_skill, AuditResult, gate
from fastapi.concurrency import run_in_threadpool
from app.services.notify import notify_version_update
from app.services.eids import normalize_eid

router = APIRouter(prefix="/api/skills", tags=["skills"])
settings = get_settings()


def _read_skill_md(skill: RpAiSkill) -> str:
    """Read the skill's SKILL.md from the repo; fall back to instructions."""
    if skill.storage_path:
        md_path = Path(settings.skill_repo_path) / skill.storage_path / "SKILL.md"
        if md_path.is_file():
            return md_path.read_text(encoding="utf-8")
    return skill.instructions or ""


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
    # Attach the deduplicated like count (favorites are unique per (eid, skill)).
    if skills:
        ids = [s.id for s in skills]
        counts = dict(
            db.execute(
                select(RpAiUserFavorite.skill_id, func.count(RpAiUserFavorite.id))
                .where(RpAiUserFavorite.skill_id.in_(ids))
                .group_by(RpAiUserFavorite.skill_id)
            ).all()
        )
        for s in skills:
            s.favorite_count = int(counts.get(s.id, 0))
    return skills


@router.get("/{skill_id}", response_model=SkillRead)
def get_skill(skill_id: str, db: Session = Depends(get_db)) -> RpAiSkill:
    skill = _get_skill_or_404(db, skill_id)
    skill.favorite_count = db.scalar(
        select(func.count())
        .select_from(RpAiUserFavorite)
        .where(RpAiUserFavorite.skill_id == skill_id)
    ) or 0
    return skill


@router.get("/{skill_id}/markdown")
def get_skill_markdown(skill_id: str, db: Session = Depends(get_db)) -> dict:
    """Return the skill's full SKILL.md content (the source of truth)."""
    skill = _get_skill_or_404(db, skill_id)
    return {"skillId": skill.id, "content": _read_skill_md(skill)}


def _artifact_archive(folder: "Path | None") -> "Path | None":
    """The uploaded artifact zip stored under ``artifacts/``, if any. This is
    the author's original skill package and the source of truth for a
    download's file layout."""
    if not folder:
        return None
    art = folder / "artifacts"
    if not art.is_dir():
        return None
    zips = sorted(p for p in art.glob("*.zip") if p.is_file())
    return zips[0] if zips else None


def _common_top_dir(members: list[zipfile.ZipInfo]) -> "str | None":
    """The single top-level directory shared by every archive member, else
    ``None``. Used to strip a redundant wrapper folder (e.g.
    ``tavily-web-search/``) so the download isn't double-nested."""
    tops: set[str] = set()
    for m in members:
        head, sep, _ = m.filename.partition("/")
        if not sep:  # a file at the archive root -> no shared wrapper
            return None
        tops.add(head)
    return next(iter(tops)) if len(tops) == 1 else None


def _bump_downloads(db: Session, skill: RpAiSkill, eid: str | None = None) -> None:
    """Record a download: log a timestamped event row (with the downloader's
    EID when known, for per-user leaderboards) and increment the cumulative
    counter. Best-effort — a logging failure must never break the download."""
    downloader = (eid or "").strip() or None
    try:
        db.add(RpAiSkillDownload(skill_id=skill.id, eid=downloader))
        skill.downloads = str(int(str(skill.downloads or "0")) + 1)
        db.commit()
    except (ValueError, TypeError):
        # Non-numeric counter (e.g. "5.6K"): still keep the event row.
        db.rollback()
        try:
            db.add(RpAiSkillDownload(skill_id=skill.id, eid=downloader))
            db.commit()
        except Exception:
            db.rollback()
    except Exception:
        db.rollback()


def _content_disposition(filename: str) -> str:
    """Build an attachment Content-Disposition with an ASCII fallback plus an
    RFC 5987 UTF-8 name, so non-ASCII (e.g. Chinese) skill names download with
    the correct filename across browsers."""
    ascii_name = filename.encode("ascii", "ignore").decode().strip() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@router.get("/{skill_id}/download")
def download_skill(
    skill_id: str,
    eid: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Download a skill as a .zip archive named after the skill's real name.

    ``eid`` (optional) records who downloaded, for per-user leaderboards; the
    endpoint stays usable anonymously when it is absent.

    The archive contains the whole skill folder (SKILL.md + any artifacts +
    manifest) under a top-level directory named after the skill. Skills with
    no folder on disk fall back to a zip containing a single SKILL.md built
    from the stored instructions.
    """
    skill = _get_skill_or_404(db, skill_id)
    # Official (anthropic) skills: name the archive after the namespaced id
    # (e.g. "anthropics-xlsx"); user skills after the display name.
    is_official = skill.category_id == "official"
    safe_name = (
        sanitize_segment(skill.id, skill_id)
        if is_official
        else sanitize_segment(skill.name, skill_id)
    )
    folder = (
        Path(settings.skill_repo_path) / skill.storage_path
        if skill.storage_path
        else None
    )

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
        archive = _artifact_archive(folder) if not is_official else None
        if archive is not None:
            try:
                with zipfile.ZipFile(archive) as src:
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
        if not wrote_any and folder and folder.is_dir():
            for p in sorted(folder.rglob("*")):
                if not p.is_file() or ".git" in p.parts:
                    continue
                rel = p.relative_to(folder)
                if (rel.parts and rel.parts[0] == "artifacts") or p.name == "manifest.json":
                    continue
                _add(str(rel), p.read_bytes())

        # 3) Nothing on disk: synthesize SKILL.md from the stored instructions.
        if not wrote_any:
            content = skill.instructions or ""
            if not content:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, "该 Skill 没有可下载的内容"
                )
            _add("SKILL.md", content.encode("utf-8"))

    buf.seek(0)
    _bump_downloads(db, skill, eid)
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


@router.put("/{skill_id}", response_model=SkillRead)
async def update_skill(
    skill_id: str, payload: SkillUpdate, db: Session = Depends(get_db)
) -> RpAiSkill:
    skill = _get_skill_or_404(db, skill_id)
    scalar_data = payload.model_dump(
        exclude_unset=True,
        exclude={"cases", "sub_skills", "input_params", "editor_eid", "skill_md"},
    )
    for key, val in scalar_data.items():
        setattr(skill, key, val)
    # author always stores the uploader's EID (identity), never a display name;
    # the human display name is resolved from the user record at read time.
    skill.author = skill.uploader_eid or payload.editor_eid or skill.author
    # When the group (category) is changed, refresh the denormalized label.
    if "category_id" in scalar_data and skill.category_id:
        cat = db.get(RpAiCategory, skill.category_id)
        if cat:
            skill.category = cat.label
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
        skill.instructions = content
        md_content = content
    else:
        md_content = render_skill_md(skill)

    db.flush()  # make edited fields/children visible for manifest building

    # The same audit as an upload, on the same content, before anything is
    # written. This route used to write straight to the skill repo: the audit was
    # attached to the upload route instead of to the act of writing, so editing a
    # skill was a way to put unaudited content in the repo.
    editor = payload.editor_eid or skill.uploader_eid
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
            f"编辑被安全审计拒绝：命中安全红线。参考编号 {decision.ref}",
        )

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
    skill.version = result.version
    skill.storage_path = result.storage_path
    skill.repo_commit = result.commit_sha
    # An edit that needs review takes the skill out of the market until the review
    # MR is merged. The reviewed-and-published content is whatever is on the live
    # branch; this edit is not it yet.
    skill.status = decision.status
    skill.audit_state = audit.state

    db.add(
        RpAiSkillVersion(
            skill_id=skill.id,
            version=result.version,
            commit_sha=result.commit_sha,
            tag=result.tag,
            uploader_eid=payload.editor_eid or owner_eid,
            notes="Edited via portal",
        )
    )

    # A review is a person reading a change, so the change has to be somewhere
    # they can read it: same Draft MR as an upload takes.
    mr_url = None
    if decision.needs_review:
        mr_url = await run_in_threadpool(
            lambda: gitlab_mr.review_suspect(
                source_branch=decision.review_branch,
                target_branch=settings.skill_repo_branch,
                skill_name=skill.name,
                findings=audit.findings,
                audit_ref=decision.ref,
            )
        )
    gate.record(db, decision.ref, audit, skill_id=skill.id,
                skill_name=skill.name, uploader_eid=editor, mr_url=mr_url)

    # Notify users who favorited this skill about the new version.
    notify_version_update(db, skill, result.version, actor_eid=payload.editor_eid)

    db.commit()
    db.refresh(skill)

    # Surface the git sync status without failing the request: the edit is
    # already saved to the DB and committed to the local repo; only the push
    # to the GitLab remote may have failed. The frontend can warn the user.
    if settings.skill_repo_push:
        skill.synced = result.pushed
        if not result.pushed:
            skill.sync_warning = (
                "改动已保存，但同步到 GitLab 远端失败，远端暂未更新；"
                "请稍后重试或联系管理员。"
            )
    return skill


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(skill_id: str, db: Session = Depends(get_db)) -> None:
    skill = _get_skill_or_404(db, skill_id)
    db.delete(skill)
    db.commit()
