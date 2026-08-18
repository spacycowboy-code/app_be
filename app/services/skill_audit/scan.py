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

import hashlib
import json
import logging
import os
import pathlib
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from app.services.skill_audit.acp import extract_json
from app.services.skill_audit.types import Finding

log = logging.getLogger(__name__)

# Findings the scanner emits about itself rather than about the skill. They mean "this
# layer did not finish", which the gate must not read as "nothing was found".
INCOMPLETE_RULES = frozenset({
    "LLM_ANALYSIS_FAILED",
    "LLM_CONTEXT_BUDGET_EXCEEDED",
    "META_ANALYSIS_FAILED",
})


# The scanner narrates its own progress on stderr. Three lines, and they land exactly on
# the boundaries between the phases, so they can drive a real progress display instead of
# a timer pretending to be one. Measured on a real skill: +3.8s, +28.2s, +79.3s of 80.7s.
STAGE_MARKERS: tuple[tuple[str, str], ...] = (
    ("Using Meta-Analyzer", "semantic"),        # deterministic analyzers done
    ("Running meta-analysis", "arbitration"),   # semantic analysis done
    ("Meta-analysis complete", "reporting"),    # arbitration done
)


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


def policy_fingerprint(policy_path: str) -> str | None:
    """Hash of the policy we scanned with.

    The scanner can attach its own fingerprint, but only in some output modes, so this is
    computed here: the policy is one of the three inputs that decide a verdict, and a
    record that cannot name it cannot be explained later.
    """
    try:
        data = pathlib.Path(policy_path).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()[:16]


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


@dataclass
class _Completed:
    returncode: int
    stdout: str
    stderr: str


def _run(cmd: list[str], *, timeout: int, env: dict[str, str],
         on_progress: Callable[[str], None] | None) -> _Completed:
    """Run the scanner, reporting each phase boundary as the scanner announces it.

    One implementation per caller shape, and the difference is deliberate. Without a
    callback this stays ``subprocess.run``: that is the path publish uses on every audit
    today, and progress reporting is not a reason to change what production already runs.
    (It shares one weakness with any timeout on a process tree — see open-items.)

    With a callback, stderr is read line by line. Three things there are not incidental:

    * **stdout goes to a file, not a pipe.** The report is on stdout and runs to tens or
      hundreds of KB. Reading stderr to EOF while stdout sits in a 64 KB pipe deadlocks as
      soon as a report outgrows that buffer: the scanner blocks writing, we block reading.
    * **The deadline is a watchdog, not a check inside the loop.** Checking the clock as
      each stderr line arrives only works while the scanner is talking. A scan that hangs
      silently would block on ``for line in proc.stderr`` forever, and the timeout is the
      whole mechanism that turns a stuck scan into "not checked, send it to review".
    * **The whole process group is killed, not just the child.** The scanner spawns its own
      subprocesses and they inherit this stderr pipe, so killing only the direct child
      leaves the pipe open and we keep waiting for the grandchild — measured: a 3 s timeout
      returned after 60 s. ``start_new_session`` makes the child a group leader so there is
      a group to kill.
    """
    if on_progress is None:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return _Completed(done.returncode, done.stdout, done.stderr)

    killed = threading.Event()
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out_file:
        proc = subprocess.Popen(cmd, stdout=out_file, stderr=subprocess.PIPE,
                                text=True, bufsize=1, env=env, start_new_session=True)

        def _expire() -> None:
            killed.set()
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

        watchdog = threading.Timer(timeout, _expire)
        watchdog.daemon = True
        watchdog.start()
        lines: list[str] = []
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                lines.append(line)
                if on_progress is None:
                    continue
                for marker, stage in STAGE_MARKERS:
                    if marker in line:
                        on_progress(stage)
                        break
            proc.wait()
        finally:
            watchdog.cancel()
            if proc.stderr is not None:
                proc.stderr.close()

        if killed.is_set():
            raise subprocess.TimeoutExpired(cmd, timeout)
        out_file.flush()
        out_file.seek(0)
        return _Completed(proc.returncode, out_file.read(), "".join(lines))


def scan(*, bin_path: str, target_dir: str, policy_path: str, timeout: int,
         env: dict[str, str] | None = None,
         on_progress: Callable[[str], None] | None = None,
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
        proc = _run(cmd, timeout=timeout, env={**os.environ, **(env or {})},
                    on_progress=on_progress)
    except subprocess.TimeoutExpired:
        log.warning("scanner timed out after %ss on %s", timeout, target_dir)
        return ScanReport(ran=False, error=f"timed out after {timeout}s")
    except OSError as exc:
        log.warning("scanner could not be run: %s", exc)
        return ScanReport(ran=False, error=str(exc))

    if not (proc.stdout or "").strip():
        # The scanner says why in one line when it refuses to load a skill at all (a
        # malformed frontmatter, for instance). That line is what the author can act on,
        # so prefer it over the tail of the log.
        stderr = (proc.stderr or "").strip()
        reason = next((ln.strip() for ln in reversed(stderr.splitlines())
                       if ln.strip().startswith("Error")), stderr[-300:])
        log.warning("scanner produced no report (rc=%s): %s", proc.returncode, reason)
        return ScanReport(ran=False, error=reason or f"no output (rc={proc.returncode})")
    # stdout is not a clean channel: when an LLM call fails, litellm prints its own
    # banner there, ahead of the report. Taking the JSON object out of the stream rather
    # than assuming the stream *is* JSON keeps a noisy dependency from turning a completed
    # scan into "no report", which the gate would read as "not checked".
    try:
        report = json.loads(extract_json(proc.stdout))
    except json.JSONDecodeError as exc:
        return ScanReport(ran=False, error=f"unparseable report: {exc}")

    findings = [_finding(f) for f in report.get("findings") or []]
    return ScanReport(
        ran=True,
        findings=findings,
        analyzers_used=list(report.get("analyzers_used") or []),
        max_severity=(report.get("max_severity") or None),
        is_safe=report.get("is_safe"),
        scanner_version=scanner_version(bin_path),
        policy_fingerprint=policy_fingerprint(policy_path),
    )
