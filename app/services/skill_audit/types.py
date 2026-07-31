"""Shared value types for the skill security-audit pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Finding:
    """One diagnostic: what was found, where it is, and what to change.

    The shape follows what compilers and linters settled on, because the person
    reading it has the same job: find the offending text and fix it. ``file`` may
    address a file inside an uploaded bundle (``bundle.zip!/scripts/x.py``), and
    ``evidence`` is the verbatim text that triggered the finding, so the report can
    point at a location instead of only naming a category.
    """

    source: str          # detect-secrets | cisco | judge | pipeline
    category: str        # secret | dangerous_instruction | ... | unclassified
    severity: str        # critical | high | medium | low
    evidence: str | None = None   # verbatim snippet (secrets already redacted)
    detail: str | None = None     # one sentence: what it means
    file: str | None = None       # where, relative to the upload
    line: int | None = None       # 1-based line in that file
    remediation: str | None = None  # what to change


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
