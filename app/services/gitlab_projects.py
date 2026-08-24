"""GitLab 写客户端:用户上传的 agent 包,每个一个项目。

`gitlab_sync` 只读(同步那两个组、拉 archive)。这个模块是唯一会**写** GitLab 的地方,
用的是另一个凭据:`rp-ai/rp-ai-user-agents` 这个组的 group access token,权限只在这一个组。

三件事值得先说清,因为它们决定了这里为什么长这样:

**为什么一个包一个项目,而不是一个大仓的一个子目录。** 包一旦是真实项目,门户已有的
`/api/market/{id}/download` 和 `/install-token` 一行都不用改 —— 它们认的就是 `gl-<项目id>`。
版本之间的 compare 链接、README 渲染、以后谁想直接 clone,全都是 GitLab 免费给的。代价是
组里会有很多小项目,那不是问题。

**为什么用 git push 而不是 commits API。** 一个 50MB 的包走 `POST /repository/commits` 要把
每个文件 base64 塞进一个 JSON 请求;git 传的是压缩后的 pack。而且 push 会经过 GitLab 的
pre-receive 钩子(包括扫密钥那个),那是我们想要的:同一个把关点,不因为换了写入方式而绕开。

**token 不进 argv,也不进 .git/config。** 用 `credential.helper=store --file=<临时文件>`:
文件 0600、和临时目录一起删。放进 remote URL 是常见做法,但那样 token 会同时出现在
`ps` 的命令行和磁盘上的 config 里,而这两处都会被别的工具顺手记下来。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)


class GitLabWriteError(RuntimeError):
    """写 GitLab 失败。`detail` 已经脱敏,可以直接给用户看。"""

    def __init__(self, detail: str, *, retryable: bool = False) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retryable = retryable


class CredentialExpired(GitLabWriteError):
    """凭据过期或被吊销。

    单独一个类型,因为它的处置和别的失败不同:重试没用,要有人去 GitLab 上换一个,
    而调用方应该把这句话原样告诉用户,而不是答一个 500。
    """


@dataclass(frozen=True)
class ProjectRef:
    """一个已存在的 GitLab 项目,门户需要的那几项。"""

    id: int
    path_with_namespace: str
    web_url: str
    http_url: str
    default_branch: str = "main"

    @property
    def item_id(self) -> str:
        """市场条目的主键。

        和同步来的条目同一种形状(`gl-<项目id>`),这不是巧合:下载与发令牌两条路径靠这个
        前缀判断"有没有代码包可取",上传出来的包正好也有。
        """
        return f"gl-{self.id}"


# --- HTTP ------------------------------------------------------------------


def _ssl_context() -> ssl.SSLContext | None:
    s = get_settings()
    if s.gitlab_verify_tls:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _opener() -> urllib.request.OpenerDirector:
    # 空 ProxyHandler:忽略环境里的 HTTP(S)_PROXY,GitLab 在内网。
    handlers: list = [urllib.request.ProxyHandler({})]
    ctx = _ssl_context()
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _token() -> str:
    s = get_settings()
    if not s.gitlab_user_agents_token:
        raise GitLabWriteError(
            "未配置上传 Agent 包所需的 GitLab 凭据(GITLAB_USER_AGENTS_TOKEN),"
            "本次上传未保存。"
        )
    return s.gitlab_user_agents_token


def _api(method: str, path: str, payload: dict | None = None) -> dict | list:
    """调 GitLab API。401/403 单独成一类,因为那是凭据问题而不是请求问题。"""
    s = get_settings()
    url = f"{s.gitlab_url.rstrip('/')}/api/v4/{path.lstrip('/')}"
    data = urllib.parse.urlencode(payload).encode() if payload else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"PRIVATE-TOKEN": _token()}
    )
    try:
        with _opener().open(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = (exc.read().decode("utf-8", "replace") or "")[:300]
        if exc.code in (401, 403):
            log.error("GitLab 拒绝了写请求(%s %s): HTTP %s", method, path, exc.code)
            raise CredentialExpired(
                "上传 Agent 包用的 GitLab 凭据已失效(过期或被吊销),请更新后重试。"
            ) from exc
        log.warning("GitLab %s %s -> HTTP %s %s", method, path, exc.code, detail)
        raise GitLabWriteError(f"GitLab 返回 HTTP {exc.code}:{detail}") from exc
    except Exception as exc:  # noqa: BLE001 - 网络/DNS/超时都是同一件事
        log.warning("连不上 GitLab(%s %s): %s", method, path, exc)
        raise GitLabWriteError("暂时连不上 GitLab,请稍后重试。", retryable=True) from exc


_namespace_id: int | None = None


def namespace_id() -> int:
    """上传组的数字 id,建项目要用。

    配置里存的是**路径**(和 sync 的两个组一致,排查时看得懂),这里解析成 id 并缓存。
    组被改名路径就失效 —— 但改名这件事本来就该有人知道,而 id 静默漂移不会。
    """
    global _namespace_id
    if _namespace_id is None:
        path = get_settings().gitlab_user_agents_group
        group = _api("GET", f"groups/{urllib.parse.quote(path, safe='')}")
        if not isinstance(group, dict) or "id" not in group:
            raise GitLabWriteError(f"找不到 GitLab 组 {path}")
        _namespace_id = int(group["id"])
    return _namespace_id


def create_project(*, slug: str, name: str, description: str | None = None) -> ProjectRef:
    """在上传组下建一个项目。

    路径撞了就在后面加序号重试:两个人上传同名 agent 是正常的,而"名字被占"不该变成
    上传失败。
    """
    base = slug
    for attempt in range(1, 6):
        path = base if attempt == 1 else f"{base}-{attempt}"
        try:
            proj = _api(
                "POST",
                "projects",
                {
                    "name": name[:255],
                    "path": path,
                    "namespace_id": str(namespace_id()),
                    "description": (description or "")[:2000],
                    # 组是 internal;项目跟着组,不要自己声明 public。
                    "visibility": "internal",
                    "initialize_with_readme": "false",
                    # 用不到的功能一律关掉:少一个入口就少一处要看的地方。
                    "issues_enabled": "false",
                    "wiki_enabled": "false",
                    "snippets_enabled": "false",
                    "container_registry_enabled": "false",
                },
            )
        except GitLabWriteError as exc:
            if "has already been taken" in str(exc) and attempt < 5:
                continue
            raise
        assert isinstance(proj, dict)
        return ProjectRef(
            id=int(proj["id"]),
            path_with_namespace=proj["path_with_namespace"],
            web_url=proj["web_url"],
            http_url=proj["http_url_to_repo"],
            default_branch=proj.get("default_branch") or "main",
        )
    raise GitLabWriteError(f"项目路径 {base} 连续 5 次都被占用")


def archive_project(project_id: int | str) -> None:
    """归档而不是删除。

    门户里删掉一行是可逆的,删掉一个仓库不是 —— 包的历史版本、谁在什么时候改了什么,
    删完再想查就没有了。归档后它从列表里消失、变成只读,该有的都还在。
    """
    _api("POST", f"projects/{project_id}/archive")


def compare_url(web_url: str, from_ref: str, to_ref: str) -> str:
    """两个版本之间的 diff 页面。审核人要看的是"这次改了什么",不是重读整包。"""
    return f"{web_url.rstrip('/')}/-/compare/{urllib.parse.quote(from_ref)}...{urllib.parse.quote(to_ref)}"


def list_tree(project_id: int | str, ref: str, *, limit: int = 500) -> list[dict]:
    """项目在某个 ref 上的文件清单。

    审核页读的是 GitLab 上**实际存在**的东西,而不是上传时我们记下的清单。两者本该一致,
    但如果有人直接在 GitLab 上改了这个项目,审核人应该看到改后的样子 —— 他要批准的是仓库
    里的内容,不是一份历史快照。
    """
    out: list[dict] = []
    page = 1
    while len(out) < limit:
        query = urllib.parse.urlencode(
            {"ref": ref, "recursive": "true", "per_page": 100, "page": page}
        )
        batch = _api("GET", f"projects/{project_id}/repository/tree?{query}")
        if not isinstance(batch, list) or not batch:
            break
        out.extend(e for e in batch if e.get("type") == "blob")
        if len(batch) < 100:
            break
        page += 1
    return out[:limit]


# --- git -------------------------------------------------------------------


def _scrub(text: str) -> str:
    """把 token 从任何要落日志或给用户看的文本里抹掉。"""
    s = get_settings()
    if s.gitlab_user_agents_token:
        text = text.replace(s.gitlab_user_agents_token, "***")
    return text


#: 环境里的代理变量,对内网 GitLab 一律不适用。
#:
#: 这不是预防性清理:实测 `HTTPS_PROXY=http://http-proxy.sero.gic.ericsson.se:8080` 在这台机器上
#: 是设着的,于是 git push 走代理、代理对内网地址回 `403 from proxy after CONNECT`。API 那边没这个
#: 问题,因为 `gitlab_sync._opener()` 装了一个空的 ProxyHandler 显式忽略这些变量 —— git 不知道
#: 那件事,所以这里要自己做一遍同样的决定。
_PROXY_VARS = (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY",
)


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _PROXY_VARS}
    # 不要弹任何交互式凭据提示:凭据只能来自我们给的那个文件。少了这个,凭据失效时 git 会
    # 挂在一个没人看得到的用户名提示上,直到 subprocess 超时 —— 那看起来像"GitLab 很慢"。
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str, cred_file: Path | None = None) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo)]
    if cred_file is not None:
        # token 只从这个文件读:不进 argv(ps 看得到),也不进 .git/config(留在磁盘上)。
        cmd += ["-c", f"credential.helper=store --file={cred_file}"]
    cmd += list(args)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=600, env=_git_env()
    )
    if proc.returncode != 0:
        raise GitLabWriteError(
            f"git {' '.join(args[:2])} 失败:{_scrub(proc.stderr.strip())[:400]}"
        )
    return proc


def publish_tree(
    project: ProjectRef,
    src: Path,
    *,
    version: str,
    message: str,
    tmp_root: Path,
) -> tuple[str, str]:
    """把 `src` 目录的内容作为项目的新一版推上去,返回 (commit_sha, tag)。

    语义是**整棵树替换**:上一版有、这一版没有的文件会被删掉。理由和安装提示词里不许
    覆盖式解压是同一个 —— 残留的旧文件仍然是能被 import 的合法模块,于是跑的是旧逻辑,
    而报错会指向一个不存在的原因。

    做法是先 fetch 到已有的 main(空项目 fetch 不到,那就是第一版),再 `reset --mixed`:
    HEAD 与索引对上远端那一版,工作区里放的是新文件,于是 `add -A` 记下的正好是
    "改了什么、删了什么"。
    """
    s = get_settings()
    work = tmp_root / "repo"
    work.mkdir(parents=True, exist_ok=True)
    cred = tmp_root / ".git-credentials"

    host = urllib.parse.urlsplit(s.gitlab_url).netloc
    # store 这个 helper 按 "协议://用户:密码@主机" 匹配;用户名任意,GitLab 只看密码。
    cred.write_text(
        f"https://oauth2:{urllib.parse.quote(_token(), safe='')}@{host}\n", encoding="utf-8"
    )
    os.chmod(cred, 0o600)

    branch = project.default_branch or "main"
    _git(work, "init", "-q", "-b", branch)
    # 身份逐条命令传,不碰全局 git config(和 skill_storage 同一条规矩)。
    ident = ["-c", f"user.name={s.git_author_name}", "-c", f"user.email={s.git_author_email}"]

    # 先把新内容铺进工作区。
    for child in src.iterdir():
        dest = work / child.name
        if child.name == ".git":
            # 上传的包里如果带着 .git,那是上传者自己的历史,不该混进这个项目 ——
            # 它会连着别的 remote 和别人的 commit 一起进来。
            continue
        if child.is_dir():
            shutil.copytree(child, dest, symlinks=False, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)

    fetched = subprocess.run(
        ["git", "-C", str(work), "-c", f"credential.helper=store --file={cred}",
         "fetch", "--depth", "1", project.http_url, branch],
        capture_output=True, text=True, timeout=600, env=_git_env(),
    )
    if fetched.returncode == 0:
        _git(work, "reset", "--mixed", "-q", "FETCH_HEAD")

    _git(work, "add", "-A")
    _git(work, *ident, "commit", "-q", "--allow-empty", "-m", message)
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()

    tag = f"v{version}"
    _git(work, *ident, "tag", "-a", tag, "-m", message)
    # 一次 push 送出分支和这个 tag。分支用显式 refspec:空项目里没有上游可依赖。
    _git(work, "push", project.http_url, f"HEAD:refs/heads/{branch}", f"refs/tags/{tag}",
         cred_file=cred)
    return sha, tag
