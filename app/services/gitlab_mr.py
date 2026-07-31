"""Open a Draft MR (with findings as a comment) for 可疑 skills.

A suspect skill's content is committed to a dedicated branch (by skill_storage),
never to the live branch. This module opens a Draft merge request from that
branch, assigns the reviewer, and posts the audit findings as a note. Merging
the MR is what promotes the skill to live.

Best-effort: without a token (or on any API error) it logs and returns None so
the upload still completes; the skill stays suspect until reviewed.
Requires a GitLab token with the `api` scope (write); the read-only sync token
(`read_api`) cannot create MRs.
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from app.config import get_settings
from app.services.skill_audit.types import Finding

log = logging.getLogger("skill_audit.mr")


def _opener() -> urllib.request.OpenerDirector:
    settings = get_settings()
    handlers: list = [urllib.request.ProxyHandler({})]  # ignore proxy (internal host)
    if not settings.gitlab_verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _request(method: str, path: str, payload: dict | None = None):
    settings = get_settings()
    if not settings.gitlab_token:
        raise RuntimeError("no GitLab token (need `api` scope for MR write)")
    url = f"{settings.gitlab_url.rstrip('/')}/api/v4/{path.lstrip('/')}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"PRIVATE-TOKEN": settings.gitlab_token,
                 "Content-Type": "application/json"},
    )
    with _opener().open(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def _project() -> str:
    return urllib.parse.quote(str(get_settings().audit_mr_project), safe="")


def _reviewer_id(username: str) -> int | None:
    try:
        users = _request("GET", f"users?username={urllib.parse.quote(username)}")
        return users[0]["id"] if users else None
    except Exception:
        return None


def _findings_markdown(skill_name: str, findings: list[Finding]) -> str:
    lines = [f"### 🔒 安全审计:`{skill_name}` 判为**可疑**,请复核后决定是否合入", ""]
    lines.append("| 位置 | 检查项 | 严重度 | 命中原文 | 说明 |")
    lines.append("|---|---|---|---|---|")
    for f in findings:
        where = f.file or "-"
        if f.line:
            where = f"{where}:{f.line}"
        where = where.replace("|", "\\|")[:120]
        ev = (f.evidence or "").replace("|", "\\|").replace("\n", " ")[:120]
        dt = (f.detail or "").replace("|", "\\|").replace("\n", " ")[:160]
        lines.append(f"| `{where}` | {f.category} | {f.severity} | {ev} | {dt} |")
    lines.append("")
    lines.append("_合入此 MR = 该 skill 上线。_")
    return "\n".join(lines)


def review_suspect(
    *, source_branch: str, target_branch: str, skill_name: str,
    findings: list[Finding], audit_ref: str,
) -> str | None:
    """Create a Draft MR from source_branch and post findings. Returns MR url."""
    settings = get_settings()
    if not settings.audit_review_enabled:
        return None
    try:
        payload = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": f"Draft: [skill-audit] 可疑复核 {skill_name} ({audit_ref})",
            "description": _findings_markdown(skill_name, findings),
            "remove_source_branch": True,
        }
        rid = _reviewer_id(settings.audit_mr_reviewer) if settings.audit_mr_reviewer else None
        if rid:
            payload["reviewer_ids"] = [rid]
        mr = _request("POST", f"projects/{_project()}/merge_requests", payload)
        mr_url = mr.get("web_url")
        iid = mr.get("iid")
        # Findings also as a note (visible in the Discussion tab).
        if iid:
            try:
                _request("POST", f"projects/{_project()}/merge_requests/{iid}/notes",
                         {"body": _findings_markdown(skill_name, findings)})
            except Exception as exc:  # note is best-effort
                log.warning("MR note failed: %s", exc)
        log.info("opened Draft MR for suspect skill '%s': %s", skill_name, mr_url)
        return mr_url
    except Exception as exc:
        log.warning("Draft MR creation failed for '%s': %s", skill_name, exc)
        return None


def mr_state(mr_url: str) -> str | None:
    """Current state of a review MR: 'opened' | 'merged' | 'closed'.

    Returns None when it cannot be determined (no token, network error, unknown
    url), so callers leave the projection untouched rather than guessing.
    """
    if not mr_url:
        return None
    m = re.search(r"/-/merge_requests/(\d+)", mr_url)
    if not m:
        return None
    try:
        data = _request("GET", f"/projects/{_project()}/merge_requests/{m.group(1)}")
    except Exception:  # network, auth, deleted MR - treat as unknown
        return None
    state = (data or {}).get("state")
    return state if state in ("opened", "merged", "closed", "locked") else None
