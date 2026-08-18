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
