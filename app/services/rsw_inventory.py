"""RSW AI Inventory file service.

Reads pre-generated HTML reports from a server-side directory whose path is
injected via RSW_INVENTORY_DIR.  The two recognised filenames are fixed; any
other name is rejected at the router level before reaching here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

# The two report types and their fixed filenames.
INVENTORY_FILES: "dict[str, str]" = {
    "full": "full_report_browser.html",
    "delta": "delta_report_browser.html",
}


def _inventory_dir() -> Path | None:
    """Return the configured directory, or None if not set."""
    raw = get_settings().rsw_inventory_dir.strip()
    return Path(raw) if raw else None


def list_files() -> list[dict]:
    """Return metadata for whichever report files actually exist."""
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
    """Return the HTML content of *filename*, or None if the file does not exist.

    Only the two fixed filenames are accepted; callers must validate before
    calling (the router does this via the whitelist check).
    """
    d = _inventory_dir()
    if d is None:
        return None
    p = d / filename
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8")
