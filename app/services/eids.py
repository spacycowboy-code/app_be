"""EID normalization: EIDs are stored and compared in UPPERCASE."""
from __future__ import annotations


def normalize_eid(eid: str | None) -> str | None:
    """Return the canonical (uppercase, trimmed) form of an EID."""
    if eid is None:
        return None
    normalized = eid.strip().upper()
    return normalized or None
