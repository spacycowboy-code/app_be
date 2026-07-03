"""Pydantic schemas for the portal user (rp_ai_portal)."""
from datetime import datetime

from app.schemas.common import CamelModel


class UserBase(CamelModel):
    eid: str
    display_name: str | None = None
    department: str | None = None


class UserCreate(UserBase):
    pass


class UserUpdate(CamelModel):
    display_name: str | None = None
    department: str | None = None


class UserRead(UserBase):
    id: int
    created_at: datetime
    updated_at: datetime
