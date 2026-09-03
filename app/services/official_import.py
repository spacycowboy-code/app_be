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

内容按引用读:先取默认分支当前的引用,再按这个引用一次拿回整个 ``official/``。原来是把一棵共享
工作副本 fetch + ``merge --ff-only`` 到最新再遍历磁盘,那次 merge 会移动整棵树的 HEAD,把并发写
请求脚下的分支抽走 —— 这是"把共享可变工作树当 API"里最难查的一条,因为它只在并发时才现形。
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.category import RpAiCategory
from app.repositories import skill_content
from app.models.skill import (
    as_download_count,
    RpAiInputParam,
    RpAiSkill,
    RpAiSkillCase,
    RpAiSubSkill,
)
from app.models.user import RpAiPortal

settings = get_settings()

OFFICIAL_DIRNAME = "official"
OFFICIAL_CATEGORY_ID = "official"
OFFICIAL_CATEGORY_LABEL = "Official"
OFFICIAL_UPLOADER_EID = "rp-ai-bot"
OFFICIAL_IMAGE = "skill-images/default.png"

# Fields owned by the marketplace runtime (accumulated as users interact); they
# must be preserved when re-importing an existing skill, never clobbered.
_RUNTIME_FIELDS = ("rating", "rating_dist", "downloads", "review_count", "is_trending")


def _ensure_bot_user(db: Session) -> None:
    if db.scalar(select(RpAiPortal).where(RpAiPortal.eid == OFFICIAL_UPLOADER_EID)) is None:
        db.add(RpAiPortal(eid=OFFICIAL_UPLOADER_EID, display_name="RP AI Bot"))


def _ensure_official_category(db: Session) -> None:
    cat = db.get(RpAiCategory, OFFICIAL_CATEGORY_ID)
    if cat is None:
        db.add(RpAiCategory(id=OFFICIAL_CATEGORY_ID, label=OFFICIAL_CATEGORY_LABEL, count=0))


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
    # 官方技能的正文同样不落库:详情接口按引用读。
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

    内容按引用读,不再碰本地工作副本。原来是 fetch + ``merge --ff-only`` 把共享工作副本拉到最新,
    失败再 ``checkout origin/main -- official/``,然后遍历磁盘 —— 那次 merge 会移动整棵树的 HEAD,
    把并发写请求脚下的分支抽走。现在先取默认分支当前引用,再按这个引用一次拿回整个 ``official/``。

    ``refresh`` 保留只为不改调用方签名,已无作用:引用总是取自远端,没有"本地那份"可以将就。
    """
    sha = skill_content.head_ref()
    tree = skill_content.read_tree(sha, OFFICIAL_DIRNAME)

    _ensure_bot_user(db)
    _ensure_official_category(db)
    db.flush()

    imported = updated = skipped = 0
    seen: list[str] = []

    # 每个技能由自己那份 manifest.json 定位,与原来遍历磁盘的口径一致:
    # 路径是相对 official/ 的,例如 "anthropics/xlsx/manifest.json"。
    for rel in sorted(k for k in tree if Path(k).name == "manifest.json"):
        folder = str(Path(rel).parent)
        try:
            manifest = json.loads(tree[rel].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            skipped += 1
            continue
        skill_id = manifest.get("id")
        if not skill_id:
            skipped += 1
            continue

        md = tree.get(f"{folder}/SKILL.md")
        instructions = md.decode("utf-8", "replace") if md is not None else None
        # Relative storage path within the repo (for downloads/artifacts),
        # e.g. "official/anthropics/xlsx".
        rel_path = f"{OFFICIAL_DIRNAME}/{folder}" if folder != "." else OFFICIAL_DIRNAME
        manifest["_storagePath"] = rel_path

        existing = db.get(RpAiSkill, skill_id)
        is_new = existing is None
        skill = existing or RpAiSkill(id=skill_id)
        if is_new:
            db.add(skill)
            # Seed runtime fields once, from the manifest defaults (0s).
            skill.rating = float(manifest.get("rating") or 0.0)
            skill.rating_dist = manifest.get("ratingDist")
            skill.downloads = as_download_count(manifest.get("downloads"))
            skill.review_count = int(manifest.get("reviewCount") or 0)
            skill.is_trending = bool(manifest.get("isTrending") or False)
        # else: preserve the existing runtime fields (do not touch them).

        _apply_manifest(
            skill, manifest, instructions,
            skill_content.version_of(rel_path, at=sha) or sha,
        )

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
