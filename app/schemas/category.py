"""Pydantic schemas for skill categories (rp_ai_category)."""
from app.schemas.common import CamelModel


class CategoryBase(CamelModel):
    id: str
    label: str
    count: int = 0


class CategoryCreate(CategoryBase):
    pass


class CategoryRead(CategoryBase):
    pass
