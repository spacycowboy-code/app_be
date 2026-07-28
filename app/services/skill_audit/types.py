"""Shared value types for the skill security-audit pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Finding:
    """One piece of evidence produced by a scanner or the judge."""

    source: str          # detect-secrets | cisco | judge | pipeline
    category: str        # secret | dangerous_instruction | ... | audit_error
    severity: str        # critical | high | medium | low
    evidence: str | None = None   # verbatim span (secrets already redacted)
    detail: str | None = None


@dataclass
class JudgeResult:
    verdict: str                       # block | flag | pass | error
    findings: list[Finding] = field(default_factory=list)
    raw: str = ""
    schema_ok: bool = True
    grounded: bool = True


@dataclass
class AuditResult:
    state: str                         # pass | suspect | danger
    verdict: str | None = None         # judge verdict
    raw: str | None = None             # raw judge output (audit trail)
    findings: list[Finding] = field(default_factory=list)
    # Checks whose layer could not run, so "no finding" must not be read as
    # "clean" (the verdict itself already fails closed to review).
    degraded: list[str] = field(default_factory=list)

    @property
    def is_danger(self) -> bool:
        return self.state == "danger"
