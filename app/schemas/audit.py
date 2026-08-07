"""API schemas for the skill security audit (evaluate + enriched upload).

`evaluate` returns the audit verdict as data (never an error), so the upload
wizard can show the real result BEFORE the user commits. The verdict itself is
produced by the pure `audit_skill()` function; publish re-runs it as the
authoritative, non-bypassable gate.
"""
from __future__ import annotations

from app.schemas.common import CamelModel


class AuditFinding(CamelModel):
    """One diagnostic: what, where, and what to change.

    ``file`` may address a file inside the uploaded bundle
    (``bundle.zip!/scripts/x.py``); ``evidence`` is the verbatim snippet that
    triggered it, with secret values already redacted upstream.
    """

    source: str            # detect-secrets | cisco | judge | pipeline
    category: str          # secret | dangerous_instruction | ... | unclassified
    severity: str          # critical | high | medium | low
    evidence: str | None = None
    detail: str | None = None
    file: str | None = None
    line: int | None = None
    remediation: str | None = None


class AuditCheck(CamelModel):
    """One fixed check in the report, with its own findings as evidence.

    The check list is closed and comes from the policy, so a clear check is
    reported too: the uploader sees what was examined, not only what failed.
    ``degraded`` means the layer behind that check could not run, so it is never
    rendered as clean.
    """

    id: str                # secret | dangerous_instruction | ...
    label: str             # user-facing label
    status: str            # clear | hit | degraded
    description: str = ""            # what this check looks for
    severity: str | None = None      # highest severity among findings, when hit
    note: str | None = None          # why the result is unknown, when degraded
    findings: list[AuditFinding] = []


class SkillEvaluateResult(CamelModel):
    """Response of the evaluate (audit-only, no store) endpoint."""

    state: str             # pass | suspect | danger
    reference: str
    checks: list[AuditCheck] = []
    findings: list[AuditFinding] = []   # flat list, kept for the audit trail
