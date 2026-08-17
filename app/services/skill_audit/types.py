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

    source: str          # the analyzer that produced it, verbatim from the scanner
    category: str        # the scanner's category, never renamed by us
    severity: str        # critical | high | medium | low | info (the scanner's scale)
    evidence: str | None = None   # verbatim snippet (secrets already redacted)
    detail: str | None = None     # one sentence: what it means
    file: str | None = None       # where, relative to the upload
    line: int | None = None       # 1-based line in that file
    remediation: str | None = None  # what to change
    rule_id: str = ""               # the scanner's rule id, so a finding is traceable
    aitech: str | None = None       # AITech taxonomy code, when the scanner supplies one


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
    # Why this outcome, in one line, for the record and the reviewer.
    reason: str = ""
    # Hash of what was audited, so a record can say whether it saw the same bytes.
    content_sha: str | None = None
    # What produced the verdict. Without these the record cannot be reproduced: a later
    # change of model, scanner or policy is indistinguishable from a change of content.
    judge_model: str | None = None
    scanner_version: str | None = None
    policy_fingerprint: str | None = None
    # Which analyzers ran, and which reported that they could not finish. Recorded so a
    # systemic slide shows up as a rate rather than as one review at a time.
    analyzers_used: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)

    @property
    def is_danger(self) -> bool:
        return self.state == "danger"

class BundleTooLarge(Exception):
    """The upload cannot be unpacked within the limits, so it is not audited.

    Not a verdict about the content: nothing was examined. The uploader has to ship
    a smaller bundle, and there is nothing for a reviewer to decide.
    """
