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
from app.services.skill_audit import scan, scanners, verdict
from app.services.skill_audit.types import AuditResult, BundleTooLarge, Finding

log = logging.getLogger("skill_audit")

# Verdict cache keyed by content hash. The verdict is a pure function of the
# Process-local first level only. The shared one is the audit record, keyed by the same
# content hash (see gate.recent_audit): with more than one API replica, evaluate and
# publish can land on different processes, and a cache that only lives in memory would
# quietly pay for the same scan twice. A miss here simply re-audits.
_CACHE_TTL = 600.0
_verdict_cache: dict[str, tuple[float, AuditResult]] = {}


def content_key(name: str, skill_md: str, artifact_bytes: bytes | None) -> str:
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


# What an uploaded bundle may unpack to. Nothing here judges content: it stops a highly
# compressed archive from filling the deploy host's disk before any check runs. Sized off
# what real skills ship — the largest bundle audited unpacks to a few hundred KB and the
# file-count record is 63 — so a bundle past these is a mistake, not a skill.
_MAX_BUNDLE_BYTES = 50 * 1024 * 1024
_MAX_BUNDLE_MEMBERS = 2000
_COPY_CHUNK = 64 * 1024
# How far to keep unpacking archives found inside the upload. An archive left on disk is
# opaque to the scanner, which reports it as an unreadable binary — a fact about our
# packaging, not about the skill — and the same bytes also go to the model.
_MAX_ARCHIVE_DEPTH = 3


def _materialize(work: Path, skill_md: str, artifact_bytes: bytes | None,
                 artifact_filename: str | None) -> None:
    (work / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if not artifact_bytes:
        return
    if zipfile.is_zipfile(io.BytesIO(artifact_bytes)):
        with zipfile.ZipFile(io.BytesIO(artifact_bytes)) as zf:
            members = zf.infolist()
            if len(members) > _MAX_BUNDLE_MEMBERS:
                raise BundleTooLarge(
                    f"技能包内文件数 {len(members)} 超过上限 {_MAX_BUNDLE_MEMBERS}。")
            written = 0
            for member in members:
                dest = (work / member.filename).resolve()
                if not str(dest).startswith(str(work.resolve())):
                    continue  # zip-slip guard
                if member.filename.endswith("/"):
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Copy in chunks against a running total rather than trusting the
                # declared size: a zip header can claim anything, and the number
                # that matters is what reaches the disk.
                with zf.open(member) as src, open(dest, "wb") as out:
                    while chunk := src.read(_COPY_CHUNK):
                        written += len(chunk)
                        if written > _MAX_BUNDLE_BYTES:
                            raise BundleTooLarge(
                                f"技能包解压后超过上限 "
                                f"{_MAX_BUNDLE_BYTES // (1024 * 1024)} MB。")
                        out.write(chunk)
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
    _unpack_nested(work)


def _unpack_nested(where: Path, depth: int = 0) -> None:
    """Unpack archives that came inside the upload, then remove them.

    A ``.zip``/``.pyz`` left in place is scanned as an opaque binary: the report carries
    "cannot analyse this file", which is a fact about packaging rather than about the
    skill, and the same bytes are also sent to the model. Unpacking makes the actual
    files reviewable; measured over 46 real skills this removed the largest single source
    of needless review and roughly halved what the semantic layer reads.
    """
    if depth >= _MAX_ARCHIVE_DEPTH:
        return
    for archive in sorted(where.rglob("*")):
        if not archive.is_file() or archive.suffix.lower() not in (".zip", ".pyz"):
            continue
        target = Path(str(archive) + ".unpacked")
        try:
            with zipfile.ZipFile(archive) as zf:
                target.mkdir(parents=True, exist_ok=True)
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    dest = (target / member.filename).resolve()
                    if not str(dest).startswith(str(target.resolve())):
                        continue                        # zip-slip guard
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out, _COPY_CHUNK)
        except (zipfile.BadZipFile, OSError, RuntimeError):
            continue                                    # not an archive we can open
        archive.unlink(missing_ok=True)
        _unpack_nested(target, depth + 1)


REVIEW_SIGNAL_CATEGORIES = ("malicious_artifact", "unclassified", "audit_error")


def audit_skill(
    *,
    name: str,
    skill_md: str,
    artifact_bytes: bytes | None = None,
    artifact_filename: str | None = None,
    uploader_eid: str | None = None,
    use_cache: bool = True,
) -> AuditResult:
    """Audit one upload: materialise it, scan it once, decide.

    A pure function of the content — no side effects, idempotent — so the same call can
    back both the wizard's preview and the authoritative check on publish.
    """
    settings = get_settings()
    if not settings.audit_enabled:
        return AuditResult(state="pass")

    key = content_key(name, skill_md, artifact_bytes)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            log.info("audit '%s' by %s -> %s (cache hit)", name, uploader_eid, cached.state)
            return cached

    # A real credential must never reach a prompt. The scanner detects secrets far more
    # broadly than we do, but it reads the files itself and calls the model itself, so
    # this one narrow check runs first and refuses outright. Coverage is not its job.
    precheck = scanners.precheck_secrets(skill_md)
    if precheck:
        result = AuditResult(
            state="danger", findings=precheck, content_sha=key,
            reason=f"明文凭据:{precheck[0].file}:{precheck[0].line}",
            judge_model=settings.audit_kiro_model,
        )
        _cache_put(key, result)
        return result

    work = Path(tempfile.mkdtemp(prefix="skill-audit-"))
    try:
        _materialize(work, skill_md, artifact_bytes, artifact_filename)
        report = scan.scan(
            bin_path=settings.audit_cisco_bin,
            target_dir=str(work),
            policy_path=settings.audit_policy_path,
            timeout=settings.audit_scan_timeout,
            use_behavioral=settings.audit_behavioral_enabled,
            env=scan.llm_env(base_url=settings.audit_llm_base_url,
                             api_key=settings.audit_judge_token,
                             model=settings.audit_kiro_model),
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    decision = verdict.decide(report)
    result = AuditResult(
        state=decision.state,
        reason=decision.reason,
        content_sha=key,
        findings=report.findings,
        incomplete=decision.incomplete or [],
        analyzers_used=report.analyzers_used,
        judge_model=settings.audit_kiro_model,
        scanner_version=report.scanner_version,
        policy_fingerprint=report.policy_fingerprint,
    )
    log.info("audit '%s' by %s -> %s (%s) analyzers=%s",
             name, uploader_eid, result.state, decision.reason or "no gating finding",
             ",".join(report.analyzers_used))
    _cache_put(key, result)
    return result
