"""RSW AI Inventory service.

Primary path: HTML reports are pushed by an external tool via POST /upload and
stored in rp_ai_inventory_report. GET /content/{filename} reads the latest row.

Fallback path (transition period): if the database has no row for a filename,
fall back to reading from RSW_INVENTORY_DIR on disk. Remove once the external
tool has completed its migration to the push API.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.inventory_report import RpAiInventoryReport

# The two report types and their fixed filenames.
INVENTORY_FILES: "dict[str, str]" = {
    "full": "full_report_browser.html",
    "delta": "delta_report_browser.html",
}


# ---------------------------------------------------------------------------
# File-based helpers (fallback / legacy)
# ---------------------------------------------------------------------------

def _inventory_dir() -> Path | None:
    raw = get_settings().rsw_inventory_dir.strip()
    return Path(raw) if raw else None


def list_files() -> list[dict]:
    """Return metadata for whichever report files actually exist on disk."""
    d = _inventory_dir()
    if d is None:
        return []
    result = []
    for report_type, filename in INVENTORY_FILES.items():
        p = d / filename
        if p.is_file():
            mtime = p.stat().st_mtime
            last_modified = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            result.append({"filename": filename, "type": report_type, "lastModified": last_modified})
    return result


def latest_modified() -> str | None:
    """ISO-8601 timestamp of the most recently modified report file, or None."""
    files = list_files()
    if not files:
        return None
    return max(f["lastModified"] for f in files)


def read_content(filename: str) -> str | None:
    """Return HTML from disk, or None if unavailable."""
    d = _inventory_dir()
    if d is None:
        return None
    p = d / filename
    return p.read_text(encoding="utf-8") if p.is_file() else None


# ---------------------------------------------------------------------------
# Database helpers (primary path)
# ---------------------------------------------------------------------------

def save_report(filename: str, content: str, db: Session) -> RpAiInventoryReport:
    """Insert a new report row and return it."""
    row = RpAiInventoryReport(filename=filename, content=content)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_files_from_db(db: Session) -> list[dict]:
    """Return metadata for filenames present in the database (latest row each)."""
    from sqlalchemy import func as sqlfunc
    filename_to_type = {v: k for k, v in INVENTORY_FILES.items()}
    rows = db.execute(
        select(
            RpAiInventoryReport.filename,
            sqlfunc.max(RpAiInventoryReport.created_at).label("latest"),
        ).group_by(RpAiInventoryReport.filename)
    ).all()
    return [
        {
            "filename": r.filename,
            "type": filename_to_type.get(r.filename, "unknown"),
            "lastModified": r.latest.isoformat(),
        }
        for r in rows
    ]


def read_content_from_db(filename: str, db: Session) -> str | None:
    """Return the HTML of the most recent upload for *filename*, or None."""
    row = db.execute(
        select(RpAiInventoryReport)
        .where(RpAiInventoryReport.filename == filename)
        .order_by(RpAiInventoryReport.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row.content if row else None


def latest_created_at(db: Session) -> str | None:
    """ISO-8601 timestamp of the most recently uploaded report, or None."""
    row = db.execute(
        select(RpAiInventoryReport.created_at)
        .order_by(RpAiInventoryReport.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.tzinfo is None:
        row = row.replace(tzinfo=timezone.utc)
    return row.isoformat()
