"""Roll the audit result up into the report the upload wizard renders.

The set of checks is fixed and closed: it comes from the policy (the red-line
list), not from whatever happened to be found. That is what makes the result a
report — the uploader can see what was examined, including the checks that came
back clear — instead of a bare list of errors.

Rules kept deliberately simple:
  * a check's ``status`` is a roll-up of its findings, never an average;
  * ``severity`` is the highest severity among that check's findings;
  * a check whose layer could not run is ``degraded``, so "we could not tell"
    is never rendered as "clean" (the verdict itself already fails closed).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.skill_audit.types import AuditResult, Finding

# id -> user-facing label. Order is the display order.
CHECKS: tuple[tuple[str, str], ...] = (
    ("secret", "Plaintext secrets"),
    ("dangerous_instruction", "Dangerous instructions"),
    ("hidden_instruction", "Hidden instructions"),
    ("prompt_injection", "Prompt injection"),
    ("malicious_artifact", "Attachment safety"),
)
_SEV_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@dataclass
class Check:
    id: str
    label: str
    status: str                      # clear | hit | degraded
    severity: str | None = None      # highest severity among findings, when hit
    findings: list[Finding] = field(default_factory=list)


def build_checks(audit: AuditResult) -> list[Check]:
    """Group findings under the fixed check list."""
    by_cat: dict[str, list[Finding]] = {}
    for f in audit.findings:
        by_cat.setdefault(f.category, []).append(f)

    checks: list[Check] = []
    for cid, label in CHECKS:
        found = by_cat.get(cid, [])
        if found:
            worst = max(found, key=lambda f: _SEV_ORDER.get(f.severity, 0))
            checks.append(Check(cid, label, "hit", worst.severity, found))
        elif cid in audit.degraded:
            checks.append(Check(cid, label, "degraded"))
        else:
            checks.append(Check(cid, label, "clear"))
    return checks
