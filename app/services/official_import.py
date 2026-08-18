"""Ingest the daily-synced *official* skills into the marketplace database.

The ``rp-ai-skill`` repo's ``official/`` folders are produced by the scheduled
sync (``.sync/sync.py`` in that repo, run by GitLab CI). Each folder holds a
``manifest.json`` (marketplace metadata) + ``SKILL.md`` + an ``artifacts/`` zip.
Nothing wrote those back into the ``rp_ai_skill`` table, so they never showed
up in the Skill Market — this service closes that gap.

Design decisions (agreed with owner):
- Scope: whatever folders currently live under ``official/`` (the 7 whitelisted
  anthropic skills). We never delete: a skill dropped upstream keeps its last
  synced row (the sync also freezes the folder), so a plain upsert suffices.
- Official skills are auto-approved (``status='approved'``) — no review flow.
- Field-level merge: sync-owned fields are overwritten from the manifest;
  runtime/marketplace fields accumulated by users are preserved on update.

The repo checkout is refreshed to ``origin/main`` first so we import the
just-pushed content (see ``_refresh_repo``). Reuses ``skill_storage``'s git
wrapper (same repo path, same per-command identity, same ``safe.directory``).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.category import RpAiCategory
from app.models.skill import (
    RpAiInputParam,
    RpAiSkill,
    RpAiSkillCase,
    RpAiSubSkill,
)
from app.models.user import RpAiPortal
from app.services.skill_storage import skill_storage

settings = get_settings()

OFFICIAL_DIRNAME = "official"
OFFICIAL_CATEGORY_ID = "official"
OFFICIAL_CATEGORY_LABEL = "Official"
OFFICIAL_UPLOADER_EID = "rp-ai-bot"
OFFICIAL_IMAGE = "skill-images/default.png"

# Fields owned by the marketplace runtime (accumulated as users interact); they
# must be preserved when re-importing an existing skill, never clobbered.
_RUNTIME_FIELDS = ("rating", "rating_dist", "downloads", "review_count", "is_trending")


def _refresh_repo() -> str | None:
    """Fast-forward the local skill repo to ``origin/main`` so ``official/`` is
    current, then return the resulting HEAD sha.

    Robust to the backend's own (pushed) upload commits: if a fast-forward is
    not possible we fall back to refreshing only the ``official/`` subtree from
    the remote ref, which never conflicts with unrelated local state.
    """
    remote = settings.skill_repo_remote
    branch = settings.skill_repo_branch
    try:
        skill_storage._git("fetch", remote, branch)
    except subprocess.CalledProcessError as exc:
        # Network/auth trouble — proceed with whatever is already on disk.
        print(f"[official-import] fetch failed, using on-disk copy: {exc.stderr or exc}")
        return skill_storage._current_sha()

    try:
        skill_storage._git("merge", "--ff-only", f"{remote}/{branch}")
    except subprocess.CalledProcessError:
        # Local main diverged (e.g. an un-pushed upload commit). Don't force a
        # merge; just materialize the latest official/ from the remote ref.
        try:
            skill_storage._git("checkout", f"{remote}/{branch}", "--", OFFICIAL_DIRNAME)
            # Leave the working tree updated but unstage so a later upload commit
            # doesn't accidentally bundle official/ changes.
            skill_storage._git("reset", "--", OFFICIAL_DIRNAME, check=False)
        except subprocess.CalledProcessError as exc:
            print(f"[official-import] official/ refresh failed: {exc.stderr or exc}")

    return skill_storage._current_sha()


def _ensure_bot_user(db: Session) -> None:
    if db.scalar(select(RpAiPortal).where(RpAiPortal.eid == OFFICIAL_UPLOADER_EID)) is None:
        db.add(RpAiPortal(eid=OFFICIAL_UPLOADER_EID, display_name="RP AI Bot"))


def _ensure_official_category(db: Session) -> None:
    cat = db.get(RpAiCategory, OFFICIAL_CATEGORY_ID)
    if cat is None:
        db.add(RpAiCategory(id=OFFICIAL_CATEGORY_ID, label=OFFICIAL_CATEGORY_LABEL, count=0))


def _folder_sha(rel_path: str, repo_sha: str | None) -> str | None:
    """The last commit that touched ``rel_path``, or ``repo_sha`` when unknown.

    ``repo_commit`` documents "the commit that last stored THIS skill", so it has
    to be resolved per skill folder. Writing the repo-wide HEAD here instead made
    every official skill's row change on any commit to the skill repo — including
    the backend's own upload commits for unrelated user skills — which bumped
    ``updated_at`` (``onupdate=func.now()``) and made all official skills appear
    on the Home page's "What's New" feed as freshly updated, with their versions
    untouched. A per-folder sha only moves when that skill's files actually move.
    """
    try:
        out = skill_storage._git(
            "log", "-1", "--format=%H", "--", rel_path, check=False
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[official-import] folder sha lookup failed for {rel_path}: {exc}")
        return repo_sha
    if out.returncode != 0:
        return repo_sha
    return out.stdout.strip() or repo_sha


def _apply_manifest(skill: RpAiSkill, manifest: dict, instructions: str | None, sha: str | None) -> None:
    """Overwrite the sync-owned fields on ``skill`` from the manifest."""
    skill.name = manifest.get("name") or skill.id
    skill.description = manifest.get("description")
    skill.capability_full = manifest.get("capabilityFull") or manifest.get("description")
    skill.category = OFFICIAL_CATEGORY_LABEL
    skill.category_id = OFFICIAL_CATEGORY_ID
    skill.tags = manifest.get("tags") or []
    skill.version = manifest.get("version")
    skill.author = manifest.get("author") or "Anthropic"
    skill.uploader_eid = OFFICIAL_UPLOADER_EID
    skill.is_free = bool(manifest.get("isFree", True))
    skill.example_prompt = manifest.get("examplePrompt")
    skill.example_output = manifest.get("exampleOutput")
    skill.instructions = instructions
    skill.status = "approved"  # official = auto-approved, no review
    skill.image = OFFICIAL_IMAGE
    skill.storage_path = manifest.get("_storagePath")
    skill.repo_commit = sha
    # Structured child collections — rebuilt wholesale (cascade delete-orphan
    # cleans out the previous rows). Official skills currently ship none.
    skill.cases = [
        RpAiSkillCase(title=c["title"], image=c.get("image"))
        for c in manifest.get("cases", [])
    ]
    skill.sub_skills = [
        RpAiSubSkill(
            name=s["name"],
            description=s.get("desc"),
            badge=s.get("badge"),
            icon_color=s.get("iconColor"),
            icon_bg=s.get("iconBg"),
        )
        for s in manifest.get("subSkills", [])
    ]
    skill.input_params = [
        RpAiInputParam(
            name=p["name"],
            type=p.get("type"),
            required=p.get("required", False),
            description=p.get("desc"),
        )
        for p in manifest.get("inputParams", [])
    ]


def import_official_skills(db: Session, *, refresh: bool = True) -> dict:
    """Scan ``official/*/manifest.json`` and upsert each into ``rp_ai_skill``.

    Returns a summary dict: ``{repoCommit, imported, updated, skipped, skills}``.
    ``refresh=False`` skips the git fetch/ff (used by dry-run tests).
    """
    repo = Path(settings.skill_repo_path)
    sha = _refresh_repo() if refresh else skill_storage._current_sha()
    official_dir = repo / OFFICIAL_DIRNAME

    _ensure_bot_user(db)
    _ensure_official_category(db)
    db.flush()

    imported = updated = skipped = 0
    seen: list[str] = []

    if official_dir.is_dir():
        # Recursively find every skill folder (identified by its manifest.json).
        # Skills are namespaced by source, e.g. official/anthropics/<skill>.
        for manifest_path in sorted(official_dir.rglob("manifest.json")):
            folder = manifest_path.parent
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                skipped += 1
                continue
            skill_id = manifest.get("id")
            if not skill_id:
                skipped += 1
                continue

            skill_md = folder / "SKILL.md"
            instructions = (
                skill_md.read_text(encoding="utf-8") if skill_md.is_file() else None
            )
            # Relative storage path within the repo (for downloads/artifacts),
            # e.g. "official/anthropics/xlsx".
            rel_path = str(folder.relative_to(repo))
            manifest["_storagePath"] = rel_path

            existing = db.get(RpAiSkill, skill_id)
            is_new = existing is None
            skill = existing or RpAiSkill(id=skill_id)
            if is_new:
                db.add(skill)
                # Seed runtime fields once, from the manifest defaults (0s).
                skill.rating = float(manifest.get("rating") or 0.0)
                skill.rating_dist = manifest.get("ratingDist")
                skill.downloads = str(manifest.get("downloads") or "0")
                skill.review_count = int(manifest.get("reviewCount") or 0)
                skill.is_trending = bool(manifest.get("isTrending") or False)
            # else: preserve the existing runtime fields (do not touch them).

            _apply_manifest(skill, manifest, instructions, _folder_sha(rel_path, sha))

            seen.append(skill_id)
            if is_new:
                imported += 1
            else:
                updated += 1

    db.commit()
    return {
        "repoCommit": sha,
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "skills": seen,
    }
