"""Deterministic layer: secret detection/redaction + static skill scanning.

Two deterministic checks, complementing the semantic judge:
- ``detect-secrets`` (in-process): broad secret formats; masks before the judge.
- Cisco AI Defense ``skill-scanner`` (external CLI, no key): the judge's blind
  spots — bundled executable scripts, .pyc bytecode, unicode-hidden instructions.

A pure-regex redactor always runs so no raw live-format secret is ever sent to
the judge, regardless of the other tools.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import zipfile
from pathlib import Path

from app.services.skill_audit.types import Finding

log = logging.getLogger("skill_audit.scanners")

# Common live-key formats. Placeholder guard: skip tvly-YOUR..., sk-YOUR...
_SECRET_PATS: list[tuple[str, re.Pattern]] = [
    ("tavily_key", re.compile(r"tvly-(?!YOUR)[A-Za-z0-9]{16,}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?(?!YOUR)[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def line_of(text: str, index: int) -> int:
    """1-based line number of a character offset."""
    return text.count("\n", 0, index) + 1



def locate(text: str, span: str) -> int | None:
    """1-based line where ``span`` first appears, or None when it does not."""
    if not span:
        return None
    i = text.find(span)
    return line_of(text, i) if i >= 0 else None



def _redact(text: str, file: str = "SKILL.md") -> tuple[str, list[Finding]]:
    """Mask any live-format secrets. Returns (redacted_text, findings)."""
    findings: list[Finding] = []
    redacted = text
    for name, pat in _SECRET_PATS:
        m = pat.search(text)
        if m:
            findings.append(
                Finding(
                    source="pipeline",
                    category="secret",
                    severity="critical",
                    # The value itself is never surfaced, only its shape.
                    evidence=f"<redacted:{name}>",
                    detail=f"A credential in {name} format is written into the file.",
                    file=file,
                    line=line_of(text, m.start()),
                )
            )
        redacted = pat.sub("<REDACTED_SECRET>", redacted)
    return redacted, findings




def precheck_secrets(text: str, file: str = "SKILL.md") -> list[Finding]:
    """Refuse before anything reaches a prompt if a real credential is written in.

    The scanner detects secrets far more broadly (`hardcoded_secrets` plus the
    promptguard provider list), so this is not the detection layer. Its only job is to
    run *before* the scanner, which reads the files itself and calls the model itself:
    a credential that is already in the text would otherwise be sent to the model. That
    a credential should not leave the place it lives does not stop applying because the
    reader is internal.
    """
    _, findings = _redact(text, file=file)
    for f in findings:
        f.category = "hardcoded_secrets"
        f.severity = "critical"
        f.rule_id = "RP_AI_PLAINTEXT_CREDENTIAL"
        f.remediation = ("Remove the credential and read it from the environment; "
                         "rotate it, since it has been committed.")
    return findings
