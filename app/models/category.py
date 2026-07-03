"""Skill category model: rp_ai_category."""
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin


class RpAiCategory(TimestampMixin, Base):
    __tablename__ = "rp_ai_category"

    # Slug id used by the frontend (e.g. "product-system", "public").
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    # Denormalized skill count for quick display; can be recomputed.
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
