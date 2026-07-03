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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models.skill import RpAiSkill
from app.models.version import RpAiSkillVersion
from app.services.skill_storage import sanitize_segment, skill_storage

settings = get_settings()
REPO = Path(settings.skill_repo_path)


def _skill_md(skill: RpAiSkill) -> str:
    """Use the skill's own instructions, or generate a definition markdown."""
    if skill.instructions and skill.instructions.strip():
        return skill.instructions

    lines = [f"# {skill.name}", ""]
    if skill.description:
        lines += [skill.description, ""]
    if skill.capability_full:
        lines += ["## 能力说明", skill.capability_full, ""]
    if skill.input_params:
        lines += ["## 输入参数"]
        for p in skill.input_params:
            req = "必填" if p.required else "可选"
            lines.append(f"- `{p.name}` ({p.type or 'string'}, {req}): {p.description or ''}")
        lines.append("")
    if skill.sub_skills:
        lines += ["## 子能力"]
        for s in skill.sub_skills:
            lines.append(f"- **{s.name}**：{s.description or ''}")
        lines.append("")
    if skill.example_prompt or skill.example_output:
        lines += ["## 示例"]
        if skill.example_prompt:
            lines += ["**Prompt**", "", f"> {skill.example_prompt}", ""]
        if skill.example_output:
            lines += ["**Output**", "", "```", skill.example_output, "```", ""]
    return "\n".join(lines)


def _manifest(skill: RpAiSkill, eid: str, slug: str) -> dict:
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "category": skill.category,
        "categoryId": skill.category_id,
        "categorySlug": slug,
        "tags": skill.tags or [],
        "version": skill.version or "1.0.0",
        "author": skill.author,
        "uploaderEid": eid,
        "rating": skill.rating,
        "downloads": skill.downloads,
        "isTrending": skill.is_trending,
        "isFree": skill.is_free,
        "ratingDist": skill.rating_dist,
        "reviewCount": skill.review_count,
        "capabilityFull": skill.capability_full,
        "examplePrompt": skill.example_prompt,
        "exampleOutput": skill.example_output,
        "status": skill.status,
        "inputParams": [
            {"name": p.name, "type": p.type, "required": p.required, "desc": p.description}
            for p in skill.input_params
        ],
        "subSkills": [
            {
                "name": s.name,
                "desc": s.description,
                "badge": s.badge,
                "iconColor": s.icon_color,
                "iconBg": s.icon_bg,
            }
            for s in skill.sub_skills
        ],
        "cases": [{"title": c.title, "image": c.image} for c in skill.cases],
        "artifactName": None,
        "storedAt": datetime.now(timezone.utc).isoformat(),
    }


def _write_skill(skill: RpAiSkill, db: Session) -> str:
    eid = skill.uploader_eid or "ericsson"
    slug = sanitize_segment(skill.category_id or "uncategorized", "uncategorized")
    rel = skill.storage_path or f"{slug}/{skill_storage.folder_name(skill.id, eid, skill.name)}"
    skill_dir = REPO / rel
    (skill_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    (skill_dir / "SKILL.md").write_text(_skill_md(skill), encoding="utf-8")
    (skill_dir / "manifest.json").write_text(
        json.dumps(_manifest(skill, eid, slug), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    skill.storage_path = rel

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
        for skill in skills:
            rel = _write_skill(skill, db)
            version = skill.version or "1.0.0"
            tags.append(f"{sanitize_segment(skill.id, 'id')}@{version}")
            print(f"  - {rel}  (v{version})")
        db.commit()

        # Commit everything in one shot.
        _git("add", "-A")
        try:
            _git("commit", "-m", f"seed: materialize {len(skills)} catalog skills")
        except subprocess.CalledProcessError as exc:
            if "nothing to commit" not in (exc.stdout or "") + (exc.stderr or ""):
                raise
            print("  (no file changes to commit)")

        # Tag each skill version (ignore already-existing tags).
        for tag in tags:
            _git("tag", "-a", tag, "-m", tag, check=False)

        sha = _git("rev-parse", "HEAD").stdout.strip()

        # Record the commit on each skill.
        for skill in skills:
            skill.repo_commit = sha
        db.commit()

        # Push branch + tags.
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
