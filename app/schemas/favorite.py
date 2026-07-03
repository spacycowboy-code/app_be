"""Pydantic schemas for user favorites (rp_ai_user_favorite)."""
from datetime import datetime

from app.schemas.common import CamelModel
from app.schemas.skill import SkillSummary


class FavoriteCreate(CamelModel):
    skill_id: str


class FavoriteRead(CamelModel):
    id: int
    eid: str
    skill_id: str
    created_at: datetime
    skill: SkillSummary | None = None
