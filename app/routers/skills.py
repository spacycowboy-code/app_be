"""Skill endpoints: browse, detail, create, update, delete."""
import io
import re
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
from app.services.notify import notify_version_update

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
    return list(db.scalars(stmt).all())


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


def _bump_downloads(db: Session, skill: RpAiSkill) -> None:
    """Best-effort increment of the download counter (only for plain ints)."""
    try:
        skill.downloads = str(int(str(skill.downloads or "0")) + 1)
        db.commit()
    except (ValueError, TypeError):
        db.rollback()


def _content_disposition(filename: str) -> str:
    """Build an attachment Content-Disposition with an ASCII fallback plus an
    RFC 5987 UTF-8 name, so non-ASCII (e.g. Chinese) skill names download with
    the correct filename across browsers."""
    ascii_name = filename.encode("ascii", "ignore").decode().strip() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@router.get("/{skill_id}/download")
def download_skill(skill_id: str, db: Session = Depends(get_db)):
    """Download a skill as a .zip archive named after the skill's real name.

    The archive contains the whole skill folder (SKILL.md + any artifacts +
    manifest) under a top-level directory named after the skill. Skills with
    no folder on disk fall back to a zip containing a single SKILL.md built
    from the stored instructions.
    """
    skill = _get_skill_or_404(db, skill_id)
    # Human-friendly base name from the skill's title (keeps unicode, safe for
    # a filename); falls back to the id if the name sanitises to nothing.
    safe_name = sanitize_segment(skill.name, skill_id)
    folder = (
        Path(settings.skill_repo_path) / skill.storage_path
        if skill.storage_path
        else None
    )

    files: list[Path] = []
    if folder and folder.is_dir():
        files = [
            p for p in folder.rglob("*") if p.is_file() and ".git" not in p.parts
        ]

    content: str | None = None
    if not files:
        # No folder on disk — fall back to the stored instructions as SKILL.md.
        content = skill.instructions or ""
        if not content:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "该 Skill 没有可下载的内容")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if files:
            for p in files:
                zf.write(p, arcname=str(Path(safe_name) / p.relative_to(folder)))
        else:
            zf.writestr(str(Path(safe_name) / "SKILL.md"), content or "")
    buf.seek(0)
    _bump_downloads(db, skill)
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
def update_skill(
    skill_id: str, payload: SkillUpdate, db: Session = Depends(get_db)
) -> RpAiSkill:
    skill = _get_skill_or_404(db, skill_id)
    scalar_data = payload.model_dump(
        exclude_unset=True,
        exclude={"cases", "sub_skills", "input_params", "editor_eid", "skill_md"},
    )
    for key, val in scalar_data.items():
        setattr(skill, key, val)
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
        action="edit",
    )
    skill.version = result.version
    skill.storage_path = result.storage_path
    skill.repo_commit = result.commit_sha

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
