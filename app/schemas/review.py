"""Pydantic schemas for skill reviews (rp_ai_skill_review)."""
from datetime import datetime

from pydantic import Field

from app.schemas.common import CamelModel


class ReviewBase(CamelModel):
    accuracy: float = Field(0.0, ge=0, le=5)
    value: float = Field(0.0, ge=0, le=5)
    docs: float = Field(0.0, ge=0, le=5)
    safety: float = Field(0.0, ge=0, le=5)
    comment: str | None = None


class ReviewCreate(ReviewBase):
    reviewer_eid: str | None = None
    is_auto: bool = False


class ReviewRead(ReviewBase):
    id: int
    skill_id: str
    reviewer_eid: str | None = None
    overall: float
    is_auto: bool
    created_at: datetime
