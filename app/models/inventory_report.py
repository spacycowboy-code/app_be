"""Inventory report model: rp_ai_inventory_report.

Each upload from the external tool inserts a new row. GET /content/{filename}
returns the most recent row for that filename, so history is preserved and
rollback is possible by deleting the latest row.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RpAiInventoryReport(Base):
    __tablename__ = "rp_ai_inventory_report"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # "full_report_browser.html" or "delta_report_browser.html"
    filename: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Full HTML content. MEDIUMTEXT on MySQL (up to 16 MB); TEXT (64 KB) is too
    # small for the full report which is already ~57 KB and will grow.
    content: Mapped[str] = mapped_column(
        Text().with_variant(MEDIUMTEXT, "mysql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
