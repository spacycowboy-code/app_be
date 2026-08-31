"""EID normalization: EIDs are stored and compared in UPPERCASE."""
from __future__ import annotations

from typing import Any


def normalize_eid(eid: str | None) -> str | None:
    """Return the canonical (uppercase, trimmed) form of an EID."""
    if eid is None:
        return None
    normalized = eid.strip().upper()
    return normalized or None


def acting_eid(request: Any, claimed: str | None) -> str | None:
    """Who to record as having done this, preferring the verified identity.

    The site-wide gate in ``app.main`` validates the bearer token and puts its subject on
    ``request.state.eid``, so that value is the caller. A client-supplied field is a claim:
    anyone signed in could put a colleague's EID in it and have the upload, the review MR
    and the audit record all attributed to that colleague.

    The claim is still the fallback, because ``auth_enabled=false`` is a supported mode
    (tests, and local runs without a directory) and there is no verified identity to use
    there. When both exist the token wins and the claim is ignored rather than rejected:
    the two disagreeing is a client bug, and failing the upload would punish the user for it.
    """
    verified = getattr(getattr(request, "state", None), "eid", None)
    return normalize_eid(verified) or normalize_eid(claimed)


def require_dashboard_eid(request: Any, action: str) -> str:
    """Only the EIDs configured for the Dashboard may do ``action``.

    The site-wide gate in ``app.main`` has already rejected anonymous callers, so
    reaching here means a valid session — just not necessarily an allowed one.

    ``action`` goes into the 403 so the message names what was refused. A shared
    "not allowed" tells the reader nothing about which of several admin surfaces
    they lack access to.

    Two hand-copied versions of this predate it — ``usage_report.require_dashboard_eid``
    and ``ivc_sync.require_sync_eid``. They are left alone here rather than
    converged in passing: this lands with a feature, and rewriting two other
    features' auth in the same change makes both harder to review. Converging
    them is worth its own change.
    """
    from fastapi import HTTPException, status

    from app.config import get_settings

    eid = str(getattr(getattr(request, "state", None), "eid", "") or "").strip().upper()
    if not eid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录。")
    if eid not in get_settings().dashboard_eid_list:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"你没有{action}的权限。")
    return eid
