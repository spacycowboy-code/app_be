"""Gerrit 上的资产内容:按不可变引用读。

与 ``skill_content`` 同一条约定 —— 调用方只说"要**哪一版**的**哪个路径**",字节从哪来是这个
模块的事,读不到就抛异常、绝不回落到数据库里的副本。区别只有两个:

* 上游是 Gerrit 的 Gitiles,不是 GitLab。厂商概念(``?format=JSON`` 的 ``)]}'`` 前缀、
  ``?format=TEXT`` 的 base64、Basic 认证)不出这个模块。
* **只读。** Gerrit 上那个仓不是门户在写,门户是它的下游。没有 ``publish`` / ``tag`` /
  ``delete_branch``,因为门户没有权限也没有理由往那边写。

为什么不 clone:``official_import`` 的注释里记着那个坑 —— 用共享工作副本 + ``merge --ff-only``
会在拉取时把并发请求脚下的分支抽走。这里从一开始就按引用读,不落工作副本。
"""

from __future__ import annotations

import base64
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request

from app.config import get_settings

log = logging.getLogger(__name__)

#: Gitiles 单个请求的超时。列一个目录树是一次请求,读一个文件也是一次。
_TIMEOUT = 30

#: 一个资产目录下允许的文件数上限。超过这个数不是资产,是代码仓 —— 与
#: ``skill_content`` 里同一条防线同样的理由:不设上限会把内存吃光。实测最大的
#: ``agent/code-reviewer`` 是 19 个顶层条目。
_MAX_TREE_ENTRIES = 2000

#: Gitiles 的 JSON 响应带这个防 JSONP 劫持的前缀,必须先剥掉才能解析。
_XSSI_PREFIX = ")]}'"

#: 取对象(目录树、文件字节)用 ``+show/``,不是 ``+/``。
#:
#: 实测:``+/<ref>/skill/rtm-3211/manifest.yaml?format=TEXT`` 是 200,而同一条路径换成
#: ``SKILL.md`` 是 **400** —— Gitiles 对 ``.md`` 走的是文档渲染那条路,与 ``format=TEXT``
#: 冲突。``+show`` 是显式的"把这个对象给我",对两者都是 200。这个坑只在真的去读一个 ``.md``
#: 时才会撞上,而技能的正文恰好全是 ``.md``。
_SHOW = "+show"


class ContentUnavailable(RuntimeError):
    """引用或路径取不到内容。让调用方明确失败,而不是悄悄换一份内容。"""


class NotFound(ContentUnavailable):
    """路径在这一版里不存在。"""


class NotConfigured(ContentUnavailable):
    """没有配 Gerrit 凭据。与"取不到"分开,因为处理方式不同:这个要去配置,不要重试。"""


def _base() -> str:
    """``https://<host>/a/plugins/gitiles/<project>`` —— 认证入口。

    ``/a/`` 前缀是 Gerrit 的认证路径;不带它的匿名路径对这个仓答 401,而 401 的正文是一整页
    HTML,解析起来只会得到一个离原因很远的错误。
    """
    settings = get_settings()
    host = settings.gerrit_base_url.rstrip("/")
    project = settings.gerrit_assets_project.strip("/")
    return f"{host}/a/plugins/gitiles/{urllib.parse.quote(project, safe='/')}"


def _opener() -> urllib.request.OpenerDirector:
    settings = get_settings()
    if not settings.gerrit_http_user or not settings.gerrit_http_password:
        raise NotConfigured(
            "Gerrit 凭据没有配置(GERRIT_HTTP_USER / GERRIT_HTTP_PASSWORD)"
        )
    ctx = ssl.create_default_context()
    if not settings.gerrit_verify_tls:
        # 与 LDAP 那边同一个取舍:内网证书链不完整,验证会让整个功能不可用。
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handler = urllib.request.HTTPSHandler(context=ctx)
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(
        None,
        settings.gerrit_base_url,
        settings.gerrit_http_user,
        settings.gerrit_http_password,
    )
    # Preemptive 不可用,所以走标准的 401-then-retry:Gerrit 会在 401 里回
    # `WWW-Authenticate: Basic realm="Gerrit Code Review"`,urllib 认这个。
    return urllib.request.build_opener(handler, urllib.request.HTTPBasicAuthHandler(mgr))


def _fetch(path_and_query: str) -> bytes:
    url = f"{_base()}/{path_and_query}"
    try:
        with _opener().open(url, timeout=_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotFound(f"Gerrit 上没有这个路径: {path_and_query}") from exc
        if exc.code in (401, 403):
            # 凭据错和没权限对调用方是同一件事,但都不该被当成"内容不存在"重试。
            raise NotConfigured(
                f"Gerrit 拒绝了这次读取(HTTP {exc.code})。凭据或项目权限不对。"
            ) from exc
        raise ContentUnavailable(f"Gerrit 返回 HTTP {exc.code}: {path_and_query}") from exc
    except urllib.error.URLError as exc:
        raise ContentUnavailable(f"连不上 Gerrit: {exc.reason}") from exc


def _json(path_and_query: str) -> dict:
    raw = _fetch(path_and_query).decode("utf-8", "replace")
    if raw.startswith(_XSSI_PREFIX):
        raw = raw[len(_XSSI_PREFIX):]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # 最常见的原因是拿到了一页登录 HTML 而不是 JSON。说出这一点,不要只报解析失败。
        raise ContentUnavailable(
            f"Gerrit 的响应不是 JSON(可能是登录页): {path_and_query}"
        ) from exc


def head_ref(branch: str | None = None) -> str:
    """某个分支当前的 commit sha —— 之后所有读都钉在这个 sha 上。

    先取引用再按引用读,是为了让一次同步看到的是**一个**版本。中途上游合入新提交,这次同步
    也不会读到半新半旧的树。
    """
    settings = get_settings()
    ref = branch or settings.gerrit_assets_branch
    # 这里要的是 commit 的元信息而不是某个对象,所以用 ``+/``。
    data = _json(f"+/refs/heads/{urllib.parse.quote(ref, safe='')}?format=JSON")
    sha = data.get("commit")
    if not sha:
        raise ContentUnavailable(f"取不到 {ref} 的 commit")
    return str(sha)


def list_dir(ref: str, path: str) -> list[dict]:
    """列一个目录的直接子项,每项 ``{name, type, id}``。

    ``type`` 是 ``tree`` 或 ``blob``,``id`` 是那个对象的 sha —— **变更判据用的就是它**。
    资产的 ``manifest.yaml`` 有一半没有版本号,所以"内容变了没有"只能问 git 自己。
    """
    p = path.strip("/")
    # 末尾的斜杠是 Gitiles 区分"列目录"和"读这个对象"的开关,少了它会拿到 commit 信息。
    suffix = f"{p}/" if p else ""
    data = _json(
        f"{_SHOW}/{urllib.parse.quote(ref, safe='')}/"
        f"{urllib.parse.quote(suffix, safe='/')}?format=JSON"
    )
    entries = data.get("entries") or []
    if len(entries) > _MAX_TREE_ENTRIES:
        raise ContentUnavailable(
            f"{path} 下有 {len(entries)} 个条目,超过 {_MAX_TREE_ENTRIES},不像一个资产目录"
        )
    return [
        {"name": e.get("name"), "type": e.get("type"), "id": e.get("id")}
        for e in entries
        if e.get("name")
    ]


def read(ref: str, path: str) -> bytes:
    """取某一版里某个文件的字节。"""
    raw = _fetch(
        f"{_SHOW}/{urllib.parse.quote(ref, safe='')}/"
        f"{urllib.parse.quote(path.strip('/'), safe='/')}?format=TEXT"
    )
    try:
        return base64.b64decode(raw)
    except (ValueError, TypeError) as exc:
        raise ContentUnavailable(f"{path} 的 base64 解不开") from exc


def read_text(ref: str, path: str) -> str:
    return read(ref, path).decode("utf-8", "replace")


def read_tree(ref: str, prefix: str) -> dict[str, bytes]:
    """把一个目录整棵读下来,``{相对 prefix 的路径: 字节}``。

    Gitiles 没有"递归列树"这一说,所以这里自己走。深度设了上限:一个资产目录再深也不该有
    十层,而没有上限的递归遇上符号链接或一个真正的代码仓会一直走下去。
    """
    out: dict[str, bytes] = {}
    stack: list[tuple[str, int]] = [(prefix.strip("/"), 0)]
    while stack:
        cur, depth = stack.pop()
        if depth > 8:
            raise ContentUnavailable(f"{prefix} 下的目录层级超过 8 层,不像一个资产")
        for entry in list_dir(ref, cur):
            child = f"{cur}/{entry['name']}" if cur else str(entry["name"])
            if entry["type"] == "tree":
                stack.append((child, depth + 1))
            elif entry["type"] == "blob":
                rel = child[len(prefix.strip("/")):].lstrip("/")
                out[rel] = read(ref, child)
            # 其余类型(commit = 子模块、symlink)跳过:门户下载不该跟着走出这个仓。
            if len(out) > _MAX_TREE_ENTRIES:
                raise ContentUnavailable(
                    f"{prefix} 下的文件超过 {_MAX_TREE_ENTRIES} 个,不像一个资产"
                )
    return out


def web_url(ref: str, path: str) -> str:
    """这个路径在 Gitiles 上的网页地址 —— 卡片上"打开"点过去的就是它。

    用分支名而不是 sha:卡片是给人看的,一个钉死在某次提交上的链接会让读者以为那是最新内容。
    """
    settings = get_settings()
    host = settings.gerrit_base_url.rstrip("/")
    project = settings.gerrit_assets_project.strip("/")
    branch = ref or settings.gerrit_assets_branch
    return (
        f"{host}/plugins/gitiles/{project}/+/refs/heads/{branch}/{path.strip('/')}"
    )
