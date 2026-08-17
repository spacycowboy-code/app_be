"""Run the skill scanner once and read its report.

The scanner is the audit engine: it runs the deterministic analyzers, then its own
semantic and meta analyzers, and returns one report. This module is the whole of our
side of that contract — invoke it, and carry its findings across **verbatim**.

Nothing here renames a category or adjusts a severity. Both are the scanner's, declared
in its rule packs and mapped to the published AITech taxonomy; a local translation table
drifts from the rule catalogue silently, and the drift shows up as either noise the gate
acts on or a real class it never sees.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field

from app.services.skill_audit.types import Finding

log = logging.getLogger(__name__)

# Findings the scanner emits about itself rather than about the skill. They mean "this
# layer did not finish", which the gate must not read as "nothing was found".
INCOMPLETE_RULES = frozenset({
    "LLM_ANALYSIS_FAILED",
    "LLM_CONTEXT_BUDGET_EXCEEDED",
    "META_ANALYSIS_FAILED",
})


@dataclass
class ScanReport:
    """What one scan produced, plus enough to know whether to trust it."""

    ran: bool                                     # did the scanner produce a report at all
    findings: list[Finding] = field(default_factory=list)
    analyzers_used: list[str] = field(default_factory=list)
    max_severity: str | None = None
    is_safe: bool | None = None
    scanner_version: str | None = None
    policy_fingerprint: str | None = None
    error: str | None = None

    @property
    def incomplete(self) -> list[str]:
        """Rules by which the scanner said a layer of itself did not finish."""
        return sorted({f.rule_id for f in self.findings
                       if f.rule_id in INCOMPLETE_RULES})


def scanner_version(bin_path: str, timeout: int = 30) -> str | None:
    try:
        proc = subprocess.run([bin_path, "--version"], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or proc.stderr or "").strip()[:120] or None


def _finding(raw: dict) -> Finding:
    """One scanner finding, carried across as-is."""
    meta = raw.get("metadata") or {}
    detail = raw.get("title") or raw.get("description")
    if raw.get("title") and raw.get("description") and raw["title"] != raw["description"]:
        detail = f"{raw['title']}: {raw['description']}"
    return Finding(
        source=raw.get("analyzer") or "scanner",
        category=str(raw.get("category") or "unclassified"),
        severity=str(raw.get("severity") or "info").lower(),
        evidence=raw.get("snippet"),
        detail=detail,
        file=raw.get("file_path"),
        line=raw.get("line_number"),
        remediation=raw.get("remediation"),
        rule_id=str(raw.get("rule_id") or ""),
        aitech=meta.get("aitech"),
    )


def llm_env(*, base_url: str, api_key: str, model: str) -> dict[str, str]:
    """How the scanner is told to reach the model.

    ``SKILL_SCANNER_LLM_MODEL`` must carry the ``openai/`` prefix: without it the
    meta-analysis path fails inside litellm with "LLM Provider NOT provided" while the
    main analysis path still works, so a misconfiguration shows up as a partial audit
    rather than an error.
    """
    return {
        "SKILL_SCANNER_LLM_PROVIDER": "openai",
        "SKILL_SCANNER_LLM_BASE_URL": base_url.rstrip("/"),
        "SKILL_SCANNER_LLM_API_KEY": api_key or "local",
        "SKILL_SCANNER_LLM_MODEL": model if model.startswith("openai/") else f"openai/{model}",
    }


def scan(*, bin_path: str, target_dir: str, policy_path: str, timeout: int,
         env: dict[str, str] | None = None,
         use_llm: bool = True, enable_meta: bool = True,
         use_behavioral: bool = False) -> ScanReport:
    """Scan a materialised skill directory.

    ``ran=False`` means we have no report — not that the skill is clean. Every caller
    has to treat that as "not checked".
    """
    cmd = [bin_path, "scan", target_dir, "--policy", policy_path, "--format", "json"]
    if use_llm:
        cmd.append("--use-llm")
    if enable_meta:
        cmd.append("--enable-meta")
    if use_behavioral:
        cmd.append("--use-behavioral")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})})
    except subprocess.TimeoutExpired:
        log.warning("scanner timed out after %ss on %s", timeout, target_dir)
        return ScanReport(ran=False, error=f"timed out after {timeout}s")
    except OSError as exc:
        log.warning("scanner could not be run: %s", exc)
        return ScanReport(ran=False, error=str(exc))

    if not (proc.stdout or "").strip():
        tail = (proc.stderr or "").strip()[-300:]
        log.warning("scanner produced no report (rc=%s): %s", proc.returncode, tail)
        return ScanReport(ran=False, error=tail or f"no output (rc={proc.returncode})")
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return ScanReport(ran=False, error=f"unparseable report: {exc}")

    findings = [_finding(f) for f in report.get("findings") or []]
    fingerprints = {(f.get("metadata") or {}).get("policy_fingerprint")
                    for f in report.get("findings") or []}
    fingerprints.discard(None)
    return ScanReport(
        ran=True,
        findings=findings,
        analyzers_used=list(report.get("analyzers_used") or []),
        max_severity=(report.get("max_severity") or None),
        is_safe=report.get("is_safe"),
        scanner_version=scanner_version(bin_path),
        policy_fingerprint=next(iter(sorted(fingerprints)), None),
    )
