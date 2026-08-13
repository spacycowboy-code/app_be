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
        # A plain .md upload *is* the definition. Writing it a second time under
        # its own name would audit the same text twice, so every finding would
        # appear once for SKILL.md and once for the uploaded filename.
        try:
            if artifact_bytes.decode("utf-8") == skill_md:
                return
        except UnicodeDecodeError:
            pass
        name = Path(artifact_filename or "artifact.bin").name
        (work / name).write_bytes(artifact_bytes)


# Text that an agent can be told to read. The semantic layer used to see only
# SKILL.md, so the same instructions moved into a bundled note or config file went
# unreviewed - the static layer keys off code patterns and does not read prose.
_TEXT_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
             ".rst", ".csv", ".properties"}
# Total budget for the extra text handed to the judge. Anything left out is
# reported, so an oversized bundle goes to review instead of passing unexamined.
_EXTRA_TEXT_BUDGET = 64 * 1024


def _collect_text_files(work: Path, main_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Bundled text files worth reviewing, plus the ones left out."""
    collected: list[tuple[str, str]] = []
    left_out: list[str] = []
    total = 0
    for path in sorted(work.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _TEXT_EXT:
            continue
        rel = str(path.relative_to(work))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            left_out.append(rel)
            continue
        if rel == "SKILL.md" or text == main_text:
            continue                        # already the main input
        if not text.strip():
            continue
        if total + len(text) > _EXTRA_TEXT_BUDGET:
            left_out.append(rel)
            continue
        total += len(text)
        collected.append((rel, text))
    return collected, left_out


def _locate_in_segments(segments: list[tuple[str, str]], span: str) -> tuple[str | None, int | None]:
    """Which bundled file a finding's span came from, and the line inside it."""
    for name, text in segments:
        line = scanners.locate(text, span)
        if line is not None:
            return name, line
    return None, None


# Findings that need a person regardless of how severe they look, because what
# they report is "this could not be judged from here": a file the audit cannot
# read, a scanner rule outside our vocabulary, a layer that did not run, two
# judges disagreeing. Severity is not the criterion — "we could not tell" is not
# a matter of degree.
REVIEW_SIGNAL_CATEGORIES = ("malicious_artifact", "unclassified", "audit_error")


def _derive_state(findings: list[Finding], judge_verdict: str) -> str:
    """Map evidence to an outcome.

    The gate acts on concealment and overreach, not on capability. A skill that
    ships scripts, installs dependencies or writes files is doing its job; that
    is recorded as context (``bundled_code``) and shown in the report, but it is
    not evidence of anything and must not consume review time. What does consume
    review time: a red line, something the audit could not read or classify, or
    anything the semantic judge itself raised.
    """
    red_line = any(
        f.category in RED_LINE_CATEGORIES and f.severity in ("critical", "high")
        for f in findings
    )
    if red_line or judge_verdict == "block":
        return "danger"
    if judge_verdict in ("flag", "error"):
        return "suspect"
    for f in findings:
        # The judge only reports what it considers a threat, and label restatements
        # are already dropped before this point, so whatever it still says stands
        # on its own evidence and goes in front of a person whatever its severity.
        if f.source == "judge":
            return "suspect"
        if f.category in REVIEW_SIGNAL_CATEGORIES:
            return "suspect"
        # Serious, but not one of the red lines: still not something to publish
        # without eyes on it.
        if f.severity in ("critical", "high"):
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
    findings += scanners.hidden_char_scan(skill_md)

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

        # Independent of the static analyser: unreadable payloads always go to
        # review, because the review is someone reading the change.
        findings += scanners.unreadable_artifact_scan(str(work))

        # A file we cannot read is already reported as an unreadable artifact,
        # with the fix the author has to make ("ship reviewable source"). A
        # third-party claim about invisible characters inside that same file
        # cannot be verified and would file the problem under hidden
        # instructions, which is not what the author must change.
        unreadable = {f.file for f in findings if f.category == "malicious_artifact"}
        findings = [f for f in findings
                    if not (f.category == "hidden_instruction"
                            and f.source != "pipeline"
                            and f.file in unreadable)]

        # Everything the agent could be told to read goes to the semantic layer,
        # not just SKILL.md, with each file's secrets masked first.
        extras, left_out = _collect_text_files(work, skill_md)
        segments: list[tuple[str, str]] = [("SKILL.md", redacted)]
        judge_input = redacted
        for rel, text in extras:
            findings += scanners.hidden_char_scan(text, file=rel)
            red_text, secret_finds = scanners.redact_secrets(text, file=rel)
            findings += secret_finds
            segments.append((rel, red_text))
            judge_input += f"\n\n----- bundled file: {rel} -----\n{red_text}"
        if left_out:
            findings.append(Finding(
                "pipeline", "unclassified", "medium",
                file=", ".join(left_out)[:400],
                detail="These bundled files were not reviewed by the semantic check "
                       "(too large or not readable as text), so this upload goes to "
                       "review.",
            ))

        judge = invoke_judge(
            settings=settings, rules=rules, skill_text=judge_input,
            model=settings.audit_kiro_model,
        )
        models_used = [settings.audit_kiro_model]

        # Rejection is the one irreversible outcome, so ask a second, different
        # model before taking it. Disagreement becomes human review rather than a
        # blocked author: model variance should cost review time, not correctness.
        confirm_model = settings.audit_kiro_model_confirm
        if judge.verdict == "block" and confirm_model and confirm_model != settings.audit_kiro_model:
            second = invoke_judge(
                settings=settings, rules=rules, skill_text=judge_input,
                model=confirm_model,
            )
            models_used.append(confirm_model)
            if second.verdict != "block":
                log.info("confirmation pass (%s) did not agree with the rejection "
                         "(%s) - routing to review", confirm_model, second.verdict)
                judge.verdict = "flag"
                # A disputed finding is not established, so it must not keep the
                # severity that makes it a red line - otherwise the downgrade is
                # cosmetic and the state derivation still rejects.
                for f in judge.findings:
                    if f.category in RED_LINE_CATEGORIES and f.severity in ("critical", "high"):
                        f.severity = "medium"
                judge.findings.append(Finding(
                    "pipeline", "unclassified", "medium",
                    detail=f"The two judges disagreed on rejecting this skill "
                           f"({settings.audit_kiro_model} rejected it, "
                           f"{confirm_model} did not), so it goes to review.",
                ))
        # Locate every judge finding in the text it was given. Done here, not in
        # the judge, so it holds whether the judge ran locally or over HTTP.
        for f in judge.findings:
            if f.evidence and not f.file:
                where, line = _locate_in_segments(segments, f.evidence)
                f.file = where or "SKILL.md"
                f.line = line if line is not None else scanners.locate(skill_md, f.evidence)
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
                         findings=findings, degraded=sorted(set(degraded)),
                         judge_model="+".join(models_used),
                         rules_hash=hashlib.sha256(rules.encode("utf-8")).hexdigest()[:16])
    if use_cache:
        _cache_put(key, result)
    return result
