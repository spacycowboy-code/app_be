"""Materialize every skill in the database into the git-managed skill repo.

For each skill it writes:

    <categorySlug>/<id>_<eid>_<name>/
        manifest.json   # full metadata (camelCase, matches the upload format)
        SKILL.md        # instructions (or a generated definition)
        artifacts/      # (empty for seeded skills)

Then it records storage_path, ensures a version row exists, commits, tags
(``<id>@<version>``) and pushes. Idempotent: re-running refreshes files and
reuses the existing storage_path / version rows.

Usage:
    python -m app.export_skills
"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models.skill import RpAiSkill
from app.models.version import RpAiSkillVersion
from app.services.skill_manifest import build_manifest, render_skill_md
from app.services.skill_storage import sanitize_segment, skill_storage

settings = get_settings()
REPO = Path(settings.skill_repo_path)


def _write_skill(skill: RpAiSkill, db: Session) -> str:
    eid = skill.uploader_eid or "ericsson"
    slug = sanitize_segment(skill.category_id or "uncategorized", "uncategorized")
    rel = skill.storage_path or f"{slug}/{skill_storage.folder_name(skill.id, eid, skill.name)}"
    skill_dir = REPO / rel
    (skill_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    (skill_dir / "SKILL.md").write_text(render_skill_md(skill), encoding="utf-8")
    manifest = build_manifest(skill, eid, slug)
    manifest["artifactName"] = None
    manifest["storedAt"] = datetime.now(timezone.utc).isoformat()
    (skill_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ``storage_path`` is not assigned here: it is written together with
    # ``repo_commit`` in ``main`` as a Core UPDATE that preserves ``updated_at``.
    # Going through the ORM would trip ``TimestampMixin.onupdate`` and make every
    # skill in the catalog look freshly updated on the Home feed.

    # Ensure a version row exists.
    version = skill.version or "1.0.0"
    exists = db.scalar(
        select(RpAiSkillVersion).where(
            RpAiSkillVersion.skill_id == skill.id,
            RpAiSkillVersion.version == version,
        )
    )
    if exists is None:
        db.add(
            RpAiSkillVersion(
                skill_id=skill.id,
                version=version,
                uploader_eid=eid,
                notes="Initial import from seed catalog",
            )
        )
    return rel


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git", "-C", str(REPO),
            "-c", f"user.name={settings.git_author_name}",
            "-c", f"user.email={settings.git_author_email}",
            *args,
        ],
        capture_output=True, text=True, check=check, timeout=180,
    )


def main() -> None:
    with SessionLocal() as db:
        skills = list(db.scalars(select(RpAiSkill).order_by(RpAiSkill.id)))
        print(f"Materializing {len(skills)} skills into {REPO} ...")
        tags: list[str] = []
        rels: dict[str, str] = {}
        for skill in skills:
            rel = _write_skill(skill, db)
            rels[skill.id] = rel
            version = skill.version or "1.0.0"
            tags.append(f"{sanitize_segment(skill.id, 'id')}@{version}")
            print(f"  - {rel}  (v{version})")
        db.commit()

        _git("add", "-A")
        try:
            _git("commit", "-m", f"seed: materialize {len(skills)} catalog skills")
        except subprocess.CalledProcessError as exc:
            if "nothing to commit" not in (exc.stdout or "") + (exc.stderr or ""):
                raise
            print("  (no file changes to commit)")

        for tag in tags:
            _git("tag", "-a", tag, "-m", tag, check=False)

        sha = _git("rev-parse", "HEAD").stdout.strip()
        # One Core UPDATE per skill, naming ``updated_at`` so it keeps its current
        # value. ``repo_commit`` gets a fresh sha on every run, so going through
        # the ORM would fire ``onupdate=func.now()`` for the whole catalog and the
        # Home feed — which reads ``updated_at > created_at`` as "this skill was
        # updated" — would announce every skill as updated each time this script
        # runs. Materializing the catalog is not a change to any skill's content.
        for skill in skills:
            db.execute(
                update(RpAiSkill)
                .where(RpAiSkill.id == skill.id)
                .values(
                    storage_path=rels[skill.id],
                    repo_commit=sha,
                    updated_at=RpAiSkill.updated_at,
                )
            )
        db.commit()

        pushed = False
        if settings.skill_repo_push:
            try:
                _git("push", settings.skill_repo_remote, f"HEAD:{settings.skill_repo_branch}")
                _git("push", settings.skill_repo_remote, "--tags")
                pushed = True
            except subprocess.CalledProcessError as exc:
                print(f"  push failed: {(exc.stderr or '').strip()}")

    print(f"Done. commit={sha[:10]} pushed={pushed}")


if __name__ == "__main__":
    main()
