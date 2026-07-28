"""Upload-time audit pipeline.

    deterministic scan (+ redact) -> semantic judge -> derived state

Order matters: secrets are scanned and redacted BEFORE the skill text is sent
to the judge, so no live secret ever leaves for the model. The pipeline fails
CLOSED — if the judge cannot run or returns junk, the skill is routed to review
(suspect), never silently passed.
"""
from __future__ import annotations

import hashlib
import io
import logging
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from app.config import get_settings
from app.models.audit import RED_LINE_CATEGORIES
from app.services.skill_audit import scanners
from app.services.skill_audit.judge import invoke_judge
from app.services.skill_audit.types import AuditResult, Finding

log = logging.getLogger("skill_audit")

# Verdict cache keyed by content hash. The verdict is a pure function of the
# skill content, so evaluate() and the authoritative re-audit in publish() can
# share one result for identical bytes instead of running the judge twice.
# In-process + TTL-bounded; a miss simply re-audits (correct, just slower).
_CACHE_TTL = 600.0
_verdict_cache: dict[str, tuple[float, AuditResult]] = {}


def _content_key(name: str, skill_md: str, artifact_bytes: bytes | None) -> str:
    h = hashlib.sha256()
    h.update((name or "").encode("utf-8"))
    h.update(b"\0")
    h.update((skill_md or "").encode("utf-8"))
    h.update(b"\0")
    if artifact_bytes:
        h.update(hashlib.sha256(artifact_bytes).digest())
    return h.hexdigest()


def _cache_get(key: str) -> AuditResult | None:
    hit = _verdict_cache.get(key)
    if hit is None:
        return None
    ts, result = hit
    if time.time() - ts > _CACHE_TTL:
        _verdict_cache.pop(key, None)
        return None
    return result


def _cache_put(key: str, result: AuditResult) -> None:
    # Bound the cache; drop the oldest entries when it grows.
    if len(_verdict_cache) > 256:
        for k in sorted(_verdict_cache, key=lambda k: _verdict_cache[k][0])[:64]:
            _verdict_cache.pop(k, None)
    _verdict_cache[key] = (time.time(), result)


def _materialize(work: Path, skill_md: str, artifact_bytes: bytes | None,
                 artifact_filename: str | None) -> None:
    (work / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if not artifact_bytes:
        return
    if zipfile.is_zipfile(io.BytesIO(artifact_bytes)):
        with zipfile.ZipFile(io.BytesIO(artifact_bytes)) as zf:
            for member in zf.namelist():
                dest = (work / member).resolve()
                if not str(dest).startswith(str(work.resolve())):
                    continue  # zip-slip guard
                if member.endswith("/"):
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(member))
    else:
        name = Path(artifact_filename or "artifact.bin").name
        (work / name).write_bytes(artifact_bytes)


def _derive_state(findings: list[Finding], judge_verdict: str) -> str:
    red_line = any(
        f.category in RED_LINE_CATEGORIES and f.severity in ("critical", "high")
        for f in findings
    )
    if red_line or judge_verdict == "block":
        return "danger"
    # Fail closed: judge error / any residual finding / explicit flag -> review.
    if judge_verdict in ("flag", "error") or findings:
        return "suspect"
    return "pass"


def audit_skill(
    *,
    name: str,
    skill_md: str,
    artifact_bytes: bytes | None = None,
    artifact_filename: str | None = None,
    uploader_eid: str | None = None,
    use_cache: bool = True,
) -> AuditResult:
    settings = get_settings()
    if not settings.audit_enabled:
        return AuditResult(state="pass")

    key = _content_key(name, skill_md, artifact_bytes)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            log.info("audit '%s' by %s -> %s (cache hit)", name, uploader_eid, cached.state)
            return cached

    try:
        rules = Path(settings.audit_rules_path).read_text(encoding="utf-8")
    except OSError as exc:
        log.error("audit rules missing (%s) — failing closed", exc)
        return AuditResult(
            state="suspect", verdict="error",
            findings=[Finding("pipeline", "audit_error", "medium",
                              detail=f"rules unreadable: {exc}")],
        )

    findings: list[Finding] = []
    degraded: list[str] = []
    redacted, secret_findings = scanners.redact_secrets(skill_md)
    findings += secret_findings

    work = Path(tempfile.mkdtemp(prefix="skill-audit-"))
    try:
        _materialize(work, skill_md, artifact_bytes, artifact_filename)

        ds_findings: list[Finding] = []
        if settings.audit_detect_secrets_enabled:
            # Scan the regex-redacted text and mask any further secrets it finds,
            # so broader-format keys never reach the semantic judge.
            ds_findings, redacted = scanners.detect_secrets_scan(redacted)
        findings += ds_findings

        # Static scan of the judge's blind spots (bundled scripts / .pyc /
        # unicode-hidden). Cisco is primary; if unavailable, fall back to the
        # cheap artifact heuristic (a deterministic backstop, logged).
        if settings.audit_cisco_enabled:
            cisco_findings, cisco_ran = scanners.cisco_scan(
                settings.audit_cisco_bin, str(work))
            if cisco_ran:
                findings += cisco_findings
            else:
                log.warning("cisco skill-scanner unavailable; using artifact heuristic fallback")
                findings += scanners.artifact_scan(str(work))
        else:
            findings += scanners.artifact_scan(str(work))

        det_summary = ", ".join(sorted({f.category for f in findings})) or "(none)"
        judge = invoke_judge(
            settings=settings, rules=rules, skill_text=redacted, det_summary=det_summary,
        )
        findings += judge.findings
        if judge.verdict == "error":
            findings.append(Finding("pipeline", "audit_error", "medium",
                                    detail="semantic judge unavailable/unparsable"))
            degraded += ["dangerous_instruction", "hidden_instruction",
                         "prompt_injection"]
    finally:
        shutil.rmtree(work, ignore_errors=True)

    state = _derive_state(findings, judge.verdict)
    log.info("audit '%s' by %s -> %s (judge=%s, %d findings)",
             name, uploader_eid, state, judge.verdict, len(findings))
    result = AuditResult(state=state, verdict=judge.verdict, raw=judge.raw,
                         findings=findings, degraded=sorted(set(degraded)))
    if use_cache:
        _cache_put(key, result)
    return result
