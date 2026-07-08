"""Seed the database with the portal's mock skill data.

Loads ``app/seed_data.json`` (exported from the frontend mock) and upserts
categories and skills (including their child records). Idempotent: running
it again refreshes existing rows without duplicating them, and without
touching unrelated data such as users, favorites or reviews.

Usage:
    python -m app.seed
"""
import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.category import RpAiCategory
from app.models.skill import (
    RpAiInputParam,
    RpAiSkill,
    RpAiSkillCase,
    RpAiSubSkill,
)
from app.models.user import RpAiPortal

SEED_FILE = Path(__file__).parent / "seed_data.json"

# All catalog skills are currently developed/owned by Ericsson.
DEVELOPER_EID = "ERICSSON"
DEVELOPER_NAME = "ERICSSON"


def _ensure_developer(db: Session) -> None:
    """Ensure the Ericsson developer account exists (skills reference it)."""
    dev = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == DEVELOPER_EID))
    if dev is None:
        db.add(RpAiPortal(eid=DEVELOPER_EID, display_name=DEVELOPER_NAME))


def _load() -> dict:
    with SEED_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


def _upsert_category(db: Session, data: dict) -> None:
    cat = db.get(RpAiCategory, data["id"])
    if cat is None:
        cat = RpAiCategory(id=data["id"])
        db.add(cat)
    cat.label = data["label"]
    cat.count = data.get("count", 0)


def _skill_scalars(data: dict) -> dict:
    """Map camelCase seed keys to RpAiSkill column kwargs."""
    return dict(
        name=data["name"],
        description=data.get("description"),
        category=data.get("category"),
        category_id=data.get("categoryId"),
        image=data.get("image"),
        rating=data.get("rating", 0.0),
        downloads=str(data.get("downloads", "0")),
        is_trending=data.get("isTrending", False),
        is_free=data.get("isFree", True),
        tags=data.get("tags", []),
        version=data.get("version"),
        author=DEVELOPER_NAME,
        uploader_eid=DEVELOPER_EID,
        capability_full=data.get("capabilityFull"),
        rating_dist=data.get("ratingDist"),
        review_count=data.get("reviewCount", 0),
        example_prompt=data.get("examplePrompt"),
        example_output=data.get("exampleOutput"),
        instructions=data.get("instructions"),
        status=data.get("status", "approved"),
    )


def _build_children(skill: RpAiSkill, data: dict) -> None:
    skill.cases = [
        RpAiSkillCase(title=c["title"], image=c.get("image"))
        for c in data.get("cases", [])
    ]
    skill.sub_skills = [
        RpAiSubSkill(
            name=s["name"],
            description=s.get("desc"),
            badge=s.get("badge"),
            icon_color=s.get("iconColor"),
            icon_bg=s.get("iconBg"),
        )
        for s in data.get("subSkills", [])
    ]
    skill.input_params = [
        RpAiInputParam(
            name=p["name"],
            type=p.get("type"),
            required=p.get("required", False),
            description=p.get("desc"),
        )
        for p in data.get("inputParams", [])
    ]


def _upsert_skill(db: Session, data: dict) -> None:
    skill = db.get(RpAiSkill, data["id"])
    if skill is None:
        skill = RpAiSkill(id=data["id"])
        db.add(skill)
    for key, val in _skill_scalars(data).items():
        setattr(skill, key, val)
    # Replace child collections wholesale (cascade delete-orphan cleans old rows).
    _build_children(skill, data)


def main() -> None:
    payload = _load()
    with SessionLocal() as db:
        _ensure_developer(db)
        for cat in payload.get("categories", []):
            _upsert_category(db, cat)
        db.flush()  # categories + developer must exist before skills reference them
        for skill in payload.get("skills", []):
            _upsert_skill(db, skill)
        db.commit()

        n_cat = db.scalar(select(func.count()).select_from(RpAiCategory))
        n_skill = db.scalar(select(func.count()).select_from(RpAiSkill))
    print(f"Seed complete: {n_cat} categories, {n_skill} skills.")


if __name__ == "__main__":
    main()
