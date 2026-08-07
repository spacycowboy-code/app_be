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

# id -> (label, what this check looks for). Order is the display order. The
# description is shown to authors who do not read security taxonomies.
CHECKS: tuple[tuple[str, str, str], ...] = (
    ("secret", "Plaintext secrets",
     "Real keys, tokens or passwords written into the skill."),
    ("dangerous_instruction", "Dangerous instructions",
     "Text that makes the assistant destroy data, send credentials out, "
     "install a foothold or bypass a permission check."),
    ("hidden_instruction", "Hidden instructions",
     "Instructions hidden with invisible characters, or letters shaped like "
     "ASCII ones."),
    ("prompt_injection", "Prompt injection",
     "Text that tells the assistant to ignore the rules it was given."),
    ("malicious_artifact", "Attachment safety",
     "Suspicious or unreviewable code in the bundled files."),
    ("unclassified", "Other findings",
     "Reported by a scanner but outside our categories, so a person decides."),
)

# What to change, per category. Finite by design: the specificity a reader needs
# comes from the location and the snippet, not from advice tailored to content we
# cannot anticipate. A scanner's own remediation, when it gives one, wins.
_REMEDIATION = {
    "secret": "Remove the credential from the file and rotate it, then read it "
              "from configuration at run time.",
    "dangerous_instruction": "Remove or narrow the instruction so the skill no "
                             "longer asks for that action.",
    "hidden_instruction": "Delete the invisible or look-alike characters and write "
                          "the text in plain characters.",
    "prompt_injection": "Remove the text that overrides the assistant's rules.",
    "malicious_artifact": "Ship reviewable source: keep scripts as plain files, do "
                          "not nest archives, and leave build output such as "
                          "__pycache__ out of the bundle.",
    "unclassified": "Read the scanner's message below and decide with the reviewer.",
    "audit_error": "Nothing to change: one check could not run, so this upload goes "
                   "to review.",
}

# Shown when a check could not be established.
_DEGRADED_NOTE = ("This check could not run for this upload, so the result is "
                  "unknown and the skill goes to review rather than through.")

_SEV_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@dataclass
class Check:
    id: str
    label: str
    status: str                      # clear | hit | degraded
    description: str = ""            # what this check looks for
    severity: str | None = None      # highest severity among findings, when hit
    note: str | None = None          # why the result is unknown, when degraded
    findings: list[Finding] = field(default_factory=list)


def build_checks(audit: AuditResult) -> list[Check]:
    """Group findings under the fixed check list."""
    by_cat: dict[str, list[Finding]] = {}
    for f in audit.findings:
        by_cat.setdefault(f.category, []).append(f)

    checks: list[Check] = []
    for cid, label, description in CHECKS:
        found = by_cat.get(cid, [])
        if found:
            for f in found:                     # fill in what to change
                if not f.remediation:
                    f.remediation = _REMEDIATION.get(f.category)
            worst = max(found, key=lambda f: _SEV_ORDER.get(f.severity, 0))
            checks.append(Check(cid, label, "hit", description,
                                severity=worst.severity, findings=found))
        elif cid in audit.degraded:
            checks.append(Check(cid, label, "degraded", description,
                                note=_DEGRADED_NOTE))
        else:
            checks.append(Check(cid, label, "clear", description))
    return checks
