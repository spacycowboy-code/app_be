"""Semantic judge: a read-only Kiro agent invoked headless via kiro-cli.

The skill content is passed in the prompt as UNTRUSTED DATA together with the
red-line rules and the deterministic findings. The judge runs tool-less
(``--trust-tools=``) so it cannot execute anything; it only emits a strict JSON
verdict. We then self-check that output (schema + evidence grounding) before
trusting it. Any failure returns verdict="error" so the pipeline fails closed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from app.services.skill_audit.types import Finding, JudgeResult

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _clean(out: str) -> str:
    out = _ANSI.sub("", out)
    lines = []
    for ln in out.splitlines():
        s = ln.rstrip()
        if s.strip() == ">":
            continue
        if s.startswith("> "):
            s = s[2:]
        lines.append(s)
    return "\n".join(lines).strip()


def _extract_json(s: str) -> dict | None:
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        return json.loads(s[i : j + 1])
    except json.JSONDecodeError:
        return None


def run_judge(
    *,
    rules: str,
    skill_text: str,
    det_summary: str,
    kiro_bin: str,
    model: str,
    work_dir: str,
    timeout: int,
) -> JudgeResult:
    prompt = (
        rules
        + "\n\n=== SKILL UNDER AUDIT ===\n" + skill_text
        + "\n\n=== DETERMINISTIC FINDINGS ===\n" + (det_summary or "(none)")
        + "\n\nNow output ONLY the JSON verdict."
    )
    cmd = [kiro_bin, "chat", "--no-interactive", "--trust-tools="]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)

    os.makedirs(work_dir, exist_ok=True)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=work_dir
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return JudgeResult(verdict="error", raw=f"invoke failed: {exc}", schema_ok=False)

    raw = _clean(proc.stdout)
    data = _extract_json(raw)
    if data is None:
        return JudgeResult(verdict="error", raw=raw[:2000], schema_ok=False)

    verdict = data.get("verdict")
    raw_findings = data.get("findings") or []
    schema_ok = verdict in ("block", "flag", "pass") and isinstance(raw_findings, list)
    if not schema_ok:
        return JudgeResult(verdict="error", raw=raw[:2000], schema_ok=False)

    findings: list[Finding] = []
    grounded = True
    for f in raw_findings:
        span = f.get("evidence_span", "") or ""
        if span and span not in skill_text:
            grounded = False  # hallucinated evidence -> don't fully trust
        findings.append(
            Finding(
                source="judge",
                category=f.get("category", "unknown"),
                severity=f.get("severity", "medium"),
                evidence=span[:500] or None,
                detail=(f.get("why") or "")[:500] or None,
            )
        )
    return JudgeResult(
        verdict=verdict, findings=findings, raw=raw[:2000],
        schema_ok=True, grounded=grounded,
    )


def _run_remote(url: str, token: str, rules: str, skill_text: str,
                det_summary: str, timeout: int, model: str = "") -> JudgeResult:
    """Call a remote judge service (POST /judge) instead of kiro-cli locally."""
    import urllib.error
    import urllib.request

    payload = json.dumps(
        {"rules": rules, "skill_text": skill_text, "det_summary": det_summary,
         "model": model}
    ).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Judge-Token"] = token
    req = urllib.request.Request(
        url.rstrip("/") + "/judge", data=payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - fail closed on any transport error
        return JudgeResult(verdict="error", raw=f"judge service error: {exc}"[:2000],
                           schema_ok=False)
    findings = [
        Finding(source=f.get("source", "judge"), category=f.get("category", "unknown"),
                severity=f.get("severity", "medium"), evidence=f.get("evidence"),
                detail=f.get("detail"))
        for f in (d.get("findings") or [])
    ]
    return JudgeResult(
        verdict=d.get("verdict", "error"), findings=findings, raw=d.get("raw", ""),
        schema_ok=d.get("schema_ok", True), grounded=d.get("grounded", True),
    )


def invoke_judge(*, settings, rules: str, skill_text: str, det_summary: str,
                 model: str | None = None) -> JudgeResult:
    """Dispatch to the remote judge service if configured, else run locally."""
    if settings.audit_judge_url:
        return _run_remote(settings.audit_judge_url, settings.audit_judge_token,
                           rules, skill_text, det_summary, settings.audit_judge_timeout,
                           model=(model or settings.audit_kiro_model))
    return run_judge(
        rules=rules, skill_text=skill_text, det_summary=det_summary,
        kiro_bin=settings.audit_kiro_bin, model=(model or settings.audit_kiro_model),
        work_dir=settings.audit_work_dir, timeout=settings.audit_judge_timeout,
    )
