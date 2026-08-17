"""Sync Agent市场 items from GitLab subgroups.

Enumerates every project inside a GitLab subgroup via the REST API and
upserts a market item per project. Uses the standard library only
(urllib) and explicitly bypasses the environment HTTP(S) proxy, which
blocks the internal GitLab host.
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass

from app.config import get_settings


class GitLabError(RuntimeError):
    """Raised when the GitLab API cannot be reached or authenticated."""


@dataclass
class GitLabProject:
    id: int
    name: str
    description: str | None
    web_url: str
    path_with_namespace: str
    topics: list[str]
    namespace_name: str | None
    default_branch: str | None
    last_activity_at: str | None


def _opener() -> urllib.request.OpenerDirector:
    # Empty ProxyHandler => ignore HTTP(S)_PROXY env vars (internal host).
    handlers: list = [urllib.request.ProxyHandler({})]
    ctx = _ssl_context()
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _ssl_context() -> ssl.SSLContext | None:
    settings = get_settings()
    if settings.gitlab_verify_tls:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _api_get(path: str, params: dict | None = None) -> tuple[list | dict, dict]:
    """GET a GitLab API path; returns (json_body, response_headers)."""
    settings = get_settings()
    if not settings.gitlab_token:
        raise GitLabError(
            "未配置 GitLab token（在 .env 设置 GITLAB_TOKEN，scope: read_api）"
        )
    url = f"{settings.gitlab_url.rstrip('/')}/api/v4/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": settings.gitlab_token})
    try:
        with _opener().open(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:200]
        raise GitLabError(f"GitLab API {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitLabError(f"无法连接 GitLab: {exc.reason}") from exc


def _api_get_raw(path: str, params: dict | None = None) -> str | None:
    """GET a raw text endpoint (e.g. a repository file); None on any error."""
    settings = get_settings()
    if not settings.gitlab_token:
        return None
    url = f"{settings.gitlab_url.rstrip('/')}/api/v4/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": settings.gitlab_token})
    try:
        with _opener().open(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "ignore")
    except Exception:
        return None


def fetch_repo_archive(project_id: int | str, ref: str | None = None) -> bytes:
    """Download a project's repository as a zip archive (bytes).

    Exists so the portal can hand a market item's code to a user who has **no
    GitLab account**: most colleagues never get access to the internal GitLab
    and do not want to request it, so "go clone the repo" is not a usable
    distribution channel. The portal's own read token fetches the archive and
    the caller only ever needs a portal login.

    **Only ever call this with an id/path that came out of our own database.**
    The portal's token can read every repo it has been granted, so passing a
    caller-supplied path would turn this into a way to read arbitrary internal
    repositories through the portal's credentials.

    Errors are raised (not swallowed like :func:`_api_get_raw`): a failed
    archive fetch must surface to the user as "download failed", never as an
    empty or truncated zip that looks like a successful download.
    """
    settings = get_settings()
    if not settings.gitlab_token:
        raise GitLabError(
            "未配置 GitLab token（在 .env 设置 GITLAB_TOKEN，scope: read_api）"
        )
    quoted = urllib.parse.quote(str(project_id), safe="")
    url = (
        f"{settings.gitlab_url.rstrip('/')}/api/v4/projects/{quoted}"
        "/repository/archive.zip"
    )
    if ref:
        url += "?" + urllib.parse.urlencode({"sha": ref})
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": settings.gitlab_token})
    try:
        # 60s rather than the 20s used for JSON calls: this transfers a whole
        # repository, and a timeout here shows up as a failed download.
        with _opener().open(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:200]
        raise GitLabError(f"GitLab archive {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitLabError(f"无法连接 GitLab: {exc.reason}") from exc


def fetch_head_commit_sha(project_id: int | str, ref: str | None = None) -> str | None:
    """Short commit sha the archive would be built from, or ``None``.

    Used as a version marker so a re-install can tell "same code, nothing to do"
    from "new code, unpack it". The sha is the right identity for that: the zip
    bytes are not guaranteed to be reproducible, and the item's ``version``
    column is hand-maintained metadata that nobody updates.

    **Failures return ``None`` instead of raising**, unlike
    :func:`fetch_repo_archive`. This is an optimisation, not correctness: without
    a version the caller reinstalls, which is what it would have done anyway. A
    download that fails must be loud; a version lookup that fails must not block
    the download it was only trying to save.

    Only ever call this with an id that came out of our own database — same rule
    as :func:`fetch_repo_archive`, for the same reason.
    """
    path = f"/projects/{urllib.parse.quote(str(project_id), safe='')}/repository/commits/"
    try:
        data, _ = _api_get(path + urllib.parse.quote(ref or "HEAD", safe=""))
    except Exception:  # noqa: BLE001 - see docstring: never block on this
        return None
    if isinstance(data, dict):
        sha = data.get("short_id") or data.get("id")
        return str(sha)[:12] if sha else None
    return None


def _clean_desc(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[*_`#]+", "", text)
    return text[:400]


def fetch_readme_description(
    project_id: int, ref: str | None, project_name: str
) -> str | None:
    """Extract the README section whose heading matches the project name.

    e.g. for repo ``doc-information`` read the text under the
    ``# doc-information`` heading, up to the next heading. If no heading
    matches the project name, return None (nothing to display).
    """
    ref = ref or "main"
    body: str | None = None
    for fname in ("README.md", "readme.md", "Readme.md", "README.MD"):
        encoded = urllib.parse.quote(fname, safe="")
        body = _api_get_raw(
            f"projects/{project_id}/repository/files/{encoded}/raw", {"ref": ref}
        )
        if body:
            break
    if not body:
        return None

    def _norm(s: str) -> str:
        return re.sub(r"[\s_-]+", "", s.strip().lower())

    target = _norm(project_name)
    lines = body.splitlines()
    heading_re = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")

    for i, line in enumerate(lines):
        m = heading_re.match(line)
        if m and _norm(m.group(2)) == target:
            buf: list[str] = []
            for nxt in lines[i + 1:]:
                if heading_re.match(nxt):
                    break
                buf.append(nxt)
            desc = _clean_desc("\n".join(buf))
            return desc or None
    return None


def list_group_projects(group_path: str) -> list[GitLabProject]:
    """List all projects under a group/subgroup (including nested subgroups)."""
    encoded = urllib.parse.quote(group_path, safe="")
    projects: list[GitLabProject] = []
    page = 1
    while True:
        body, headers = _api_get(
            f"groups/{encoded}/projects",
            {
                "include_subgroups": "true",
                "per_page": "100",
                "page": str(page),
                "archived": "false",
                "order_by": "name",
                "sort": "asc",
            },
        )
        if not isinstance(body, list):
            break
        for p in body:
            ns = (p.get("namespace") or {}).get("name")
            projects.append(
                GitLabProject(
                    id=p["id"],
                    name=p.get("name") or p.get("path") or f"project-{p['id']}",
                    description=p.get("description"),
                    web_url=p.get("web_url") or "",
                    path_with_namespace=p.get("path_with_namespace") or "",
                    topics=p.get("topics") or p.get("tag_list") or [],
                    namespace_name=ns,
                    default_branch=p.get("default_branch"),
                    last_activity_at=p.get("last_activity_at"),
                )
            )
        next_page = headers.get("X-Next-Page") or headers.get("x-next-page")
        if not next_page:
            break
        page = int(next_page)
    return projects
