"""RSW AI Inventory endpoints.

Three read-only endpoints that expose pre-generated HTML reports placed on the
server by an external tool.  The path is configured via RSW_INVENTORY_DIR; if
the variable is unset or the files are absent every endpoint degrades gracefully
(empty list / null / 404) rather than erroring.

TODO: add /api/rsw/inventory/content/{filename} to AUTH_WHITELIST in main.py
      if unauthenticated access is ever needed (e.g. embedding in another tool).
      For now the standard JWT gate applies.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse

from app.services.rsw_inventory import INVENTORY_FILES, list_files, latest_modified, read_content

router = APIRouter(prefix="/api/rsw/inventory", tags=["rsw-inventory"])

# Only these two filenames may be requested via the content endpoint.
_ALLOWED_FILENAMES = frozenset(INVENTORY_FILES.values())


@router.get("/files")
def get_files() -> list[dict]:
    """List whichever report files currently exist on disk.

    Returns an empty list when RSW_INVENTORY_DIR is unset or no files are
    present — the frontend treats this as the empty state, not an error.
    """
    return list_files()


@router.get("/status")
def get_status() -> dict:
    """Timestamp of the most recently modified report file.

    ``lastUpdated`` is null when no files exist.  The frontend polls this
    endpoint (24 h interval) and reloads content only when the value changes.
    """
    return {"lastUpdated": latest_modified()}


@router.get("/content/{filename}", response_class=HTMLResponse)
def get_content(filename: str) -> HTMLResponse:
    """Return the raw HTML of the requested report file.

    Only the two fixed filenames are accepted; anything else is a 404.
    The HTML is returned as-is and rendered via ``srcdoc`` in the frontend
    iframe, so no sanitisation is applied here — the files are operator-supplied
    and trusted.
    """
    if filename not in _ALLOWED_FILENAMES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown inventory file '{filename}'.")
    content = read_content(filename)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Inventory file not available.")
    return HTMLResponse(content=content)
