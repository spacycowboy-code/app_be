"""Schemas for skill comments and workspace activities."""
from datetime import datetime

from app.schemas.common import CamelModel


class CommentCreate(CamelModel):
    author_eid: str
    content: str


class CommentRead(CamelModel):
    id: int
    skill_id: str
    author_eid: str
    content: str
    created_at: datetime


class ActivityRead(CamelModel):
    id: int
    type: str
    skill_id: str | None = None
    skill_name: str | None = None
    actor_eid: str | None = None
    version: str | None = None
    message: str
    is_read: bool
    created_at: datetime
