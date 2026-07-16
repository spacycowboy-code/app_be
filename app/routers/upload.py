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
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.category import RpAiCategory
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.models.version import RpAiSkillVersion
from app.schemas.version import SkillUploadResult, SkillVersionRead
from app.services.skill_manifest import (
    extract_skill_md_from_zip,
    has_name_and_description,
    parse_frontmatter,
)
from app.services.skill_storage import skill_storage
from app.services.notify import notify_version_update
from app.services.eids import normalize_eid

router = APIRouter(prefix="/api/skills", tags=["upload"])
settings = get_settings()


def _category_image(category_id: str) -> str:
    """Frontend image asset for a category group."""
    # Public groups ('public', 'bnew') have no dedicated image; reuse tavily art.
    slug = category_id or "product-system"
    if slug in ("public", "bnew"):
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


@router.post(
    "/upload", response_model=SkillUploadResult, status_code=status.HTTP_201_CREATED
)
async def upload_skill(
    db: Session = Depends(get_db),
    eid: str = Form(..., description="Uploader EID"),
    name: str = Form(...),
    category_id: str = Form(..., alias="categoryId"),
    description: str | None = Form(None),
    author: str | None = Form(None, description="Display name; defaults to owner EID"),
    tags: str | None = Form(None, description="JSON array or comma-separated"),
    instructions: str | None = Form(None),
    skill_id: str | None = Form(None, alias="id"),
    file: UploadFile | None = File(None),
) -> SkillUploadResult:
    eid = normalize_eid(eid)
    if not eid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "eid is required")

    # Read + size-check the artifact.
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
        # Try to pull SKILL.md out of the archive so the definition can be
        # validated and stored, while the zip itself is kept as the artifact.
        if file_text is None:
            file_text = extract_skill_md_from_zip(artifact_bytes)

    # The skill definition (SKILL.md) comes from the uploaded file, or the
    # instructions field as a fallback. It MUST declare name: and description:.
    definition = file_text if file_text is not None else instructions
    if not definition or not has_name_and_description(definition):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Skill 定义必须包含 name: 和 description: 两个字段",
        )

    # The card/description shown in the market comes from the SKILL.md
    # `description:` frontmatter; the (optional) form field only overrides it.
    _, fm_description = parse_frontmatter(definition)
    effective_description = description if description is not None else fm_description

    # Resolve identity: existing skill (by id) -> re-upload/version bump.
    existing: RpAiSkill | None = None
    if skill_id:
        existing = db.get(RpAiSkill, skill_id)
    resolved_id = skill_id or _slugify(name)
    if existing is None and db.get(RpAiSkill, resolved_id) is not None:
        # New upload but slug collides with another skill; make it unique.
        resolved_id = f"{resolved_id}-{uuid.uuid4().hex[:6]}"

    # Category: category_id is the slug; look up its display label.
    category = db.get(RpAiCategory, category_id)
    category_label = category.label if category else category_id

    _ensure_user(db, eid)

    # Build manifest metadata (persisted alongside the files).
    parsed_tags = _parse_tags(tags)
    manifest = {
        "name": name,
        "description": effective_description,
        "author": (author or "").strip() or (existing.uploader_eid if existing else eid),
        "category": category_label,
        "categoryId": category_id,
        "tags": parsed_tags,
    }

    # The uploader on record is the original creator; keep it stable.
    owner_eid = existing.uploader_eid if existing else eid

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
    )

    # Upsert the skill row.
    skill = existing
    if skill is None:
        skill = RpAiSkill(id=resolved_id, uploader_eid=eid, tags=[])
        db.add(skill)
    skill.name = name
    if effective_description is not None:
        skill.description = effective_description
    # Display name (author): use the provided value, otherwise fall back to the
    # uploader/owner EID (only when not already set on an existing skill).
    display_name = (author or "").strip()
    if display_name:
        skill.author = display_name
    elif not skill.author:
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
    # Freshly uploaded skills must pass review before they are approved.
    skill.status = "pending"

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

    db.commit()
    db.refresh(skill)

    return SkillUploadResult(
        skill=skill,
        version=result.version,
        commit_sha=result.commit_sha,
        tag=result.tag,
        storage_path=result.storage_path,
        pushed=result.pushed,
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
