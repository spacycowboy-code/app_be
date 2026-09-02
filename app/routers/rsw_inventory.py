"""RSW AI Inventory endpoints.

POST /upload   — external tool pushes HTML; authenticated by X-API-Key.
GET  /files    — list available report types (checks DB first, falls back to disk).
GET  /status   — latest upload timestamp (DB first, falls back to disk).
GET  /content/{filename} — return HTML (DB first, falls back to disk).
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.services.rsw_inventory import (
    INVENTORY_FILES,
    latest_created_at,
    latest_modified,
    list_files,
    list_files_from_db,
    read_content,
    read_content_from_db,
    save_report,
)

router = APIRouter(prefix="/api/rsw/inventory", tags=["rsw-inventory"])

_ALLOWED_FILENAMES = frozenset(INVENTORY_FILES.values())

# ~20 MB in characters — generous upper bound for HTML reports.
_MAX_CONTENT_CHARS = 20 * 1024 * 1024


class UploadRequest(BaseModel):
    filename: str
    content: str = Field(max_length=_MAX_CONTENT_CHARS)


@router.post("/upload", status_code=status.HTTP_201_CREATED)
def upload_report(
    body: UploadRequest,
    x_api_key: str = Header(default=""),
    db: Session = Depends(get_db),
) -> dict:
    """Receive an HTML report from the external inventory tool.

    Authenticated by ``X-API-Key`` header (not JWT — this is a machine caller).
    The endpoint must be in AUTH_WHITELIST in main.py so the JWT gate lets it
    through to this check.
    """
    cfg = get_settings()
    # Constant-time comparison prevents timing attacks; also rejects empty key config.
    if not cfg.rsw_inventory_api_key or not hmac.compare_digest(
        x_api_key, cfg.rsw_inventory_api_key
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or missing API key.")
    if body.filename not in _ALLOWED_FILENAMES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown filename '{body.filename}'. Allowed: {sorted(_ALLOWED_FILENAMES)}",
        )
    row = save_report(body.filename, body.content, db)
    return {"id": row.id, "filename": row.filename, "createdAt": row.created_at.isoformat()}


@router.get("/files")
def get_files(db: Session = Depends(get_db)) -> list[dict]:
    """List available report types.

    Checks the database first; falls back to disk for the transition period.
    Returns an empty list when neither source has data.
    """
    db_files = list_files_from_db(db)
    return db_files if db_files else list_files()


@router.get("/status")
def get_status(db: Session = Depends(get_db)) -> dict:
    """Timestamp of the most recently uploaded report (DB first, then disk)."""
    ts = latest_created_at(db) or latest_modified()
    return {"lastUpdated": ts}


@router.get("/content/{filename}", response_class=HTMLResponse)
def get_content(filename: str, db: Session = Depends(get_db)) -> HTMLResponse:
    """Return the raw HTML of the requested report.

    Reads the most recent database row first; falls back to disk if the DB has
    no row yet (transition period). Only the two fixed filenames are accepted.
    """
    if filename not in _ALLOWED_FILENAMES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown inventory file '{filename}'.")
    content = read_content_from_db(filename, db) or read_content(filename)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Inventory file not available.")
    return HTMLResponse(content=content)
