"""API schemas for the skill security audit (evaluate + enriched upload).

`evaluate` returns the audit verdict as data (never an error), so the upload
wizard can show the real result BEFORE the user commits. The verdict itself is
produced by the pure `audit_skill()` function; publish re-runs it as the
authoritative, non-bypassable gate.
"""
from __future__ import annotations

from app.schemas.common import CamelModel


class AuditFinding(CamelModel):
    """One human-readable finding. Secret values are already redacted upstream."""

    source: str            # detect-secrets | cisco | judge | pipeline
    category: str          # secret | dangerous_instruction | ... | audit_error
    severity: str          # critical | high | medium | low
    evidence: str | None = None
    detail: str | None = None


class SkillEvaluateResult(CamelModel):
    """Response of the evaluate (audit-only, no store) endpoint."""

    state: str             # pass | suspect | danger
    reference: str
    findings: list[AuditFinding] = []
