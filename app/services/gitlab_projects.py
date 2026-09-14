"""GitLab 写客户端:用户上传的包(tool / agent),每个一个项目。

`gitlab_sync` 只读(同步那两个组、拉 archive)。这个模块是唯一会**写** GitLab 的地方,
用的是另一套凭据:两个上传组各自的 group access token,权限只在那一个组 ——
agent 进 `rp-ai/rp-ai-user-agents`,tool 进 `rp-ai/rp-ai-user-tools`。哪个组、哪个 token
是同一个决定,所以它们一起装在 `UploadTarget` 里传,不分开传。

四件事值得先说清,因为它们决定了这里为什么长这样:

**为什么一个包一个项目,而不是一个大仓的一个子目录。** 包一旦是真实项目,门户已有的
`/api/market/{id}/download` 和 `/install-token` 一行都不用改 —— 它们认的就是 `gl-<项目id>`,
而 GitLab 的项目 id 全局唯一,和它在哪个组无关。版本之间的 compare 链接、README 渲染、
以后谁想直接 clone,全都是 GitLab 免费给的。代价是组里会有很多小项目,那不是问题。

**为什么两个组两个 token,而不是一个覆盖父组的。** 一个 `rp-ai` 上的 token 只要一个变量,
代价是写权限扩到整个 `rp-ai`(含技能仓和 sync 的那两个组)—— 正好是当初把这个凭据从个人
PAT 拆出来要避免的事。谁能写哪里,应该和"哪个功能需要写哪里"一样窄。

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
from dataclasses import dataclass, replace
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)


class GitLabWriteError(RuntimeError):
    """写 GitLab 失败。`detail` 已经脱敏,可以直接给用户看。

    除了给人看的 `detail`,还带上**给代码看的**两样东西:HTTP 状态码,以及 GitLab 在校验
    失败时逐字段给出的错误(`{"name": [...], "path": [...]}`)。

    为什么这很重要:GitLab 明确告诉了我们是**哪个字段**出问题,而原来这里只留下一个拼好的
    字符串,调用方只能对整体做子串匹配。于是建项目时分不清"路径被占"和"名字被占",只好把
    两个一起改 —— 一个本来合法的名字因此被加上了 GitLab 不接受的后缀,最终的报错是"名字有
    非法字符",把人指向一个跟真实原因无关的方向。
    """

    def __init__(
        self,
        detail: str,
        *,
        retryable: bool = False,
        status: int | None = None,
        errors: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retryable = retryable
        #: HTTP 状态码。None 表示请求根本没发出去(网络层失败)。
        self.status = status
        #: GitLab 的逐字段校验错误,字段名 → 消息列表。非校验类失败时为空。
        self.errors = errors or {}

    def taken(self, *fields: str) -> bool:
        """这些字段里有没有哪个被 GitLab 判为"已被占用"。"""
        return any(
            "has already been taken" in msg
            for field in fields
            for msg in self.errors.get(field, ())
        )

    def rejected(self, field: str) -> bool:
        """这个字段被拒了,但**不是**因为被占用 —— 也就是格式/字符集不合它的规矩。"""
        msgs = self.errors.get(field) or []
        return bool(msgs) and not self.taken(field)


class CredentialExpired(GitLabWriteError):
    """凭据过期或被吊销。

    单独一个类型,因为它的处置和别的失败不同:重试没用,要有人去 GitLab 上换一个,
    而调用方应该把这句话原样告诉用户,而不是答一个 500。
    """


class PushRuleRejected(GitLabWriteError):
    """GitLab 的推送规则拒了这次 push。

    和别的写失败分开,因为它既不是我们的错也不是上传者的错:内容没问题、凭据没问题,是目标
    项目上的一条规则不允许这种提交。重试同一份内容一定还是被拒,所以调用方必须把"要谁去改
    什么"说出来,而不是答一个 502 加一段 git 原文 —— 那是上传者唯一看到的东西,而他对它无能
    为力。
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


@dataclass(frozen=True)
class UploadTarget:
    """一个上传目标:哪个组、用哪个 token、缺了要报哪个变量名。

    存在的理由是**每个组一个 token**:group access token 只能访问它所属的那个组,所以
    "包放哪个组"和"用哪个凭据"是同一个决定,不能分开传。改成一个覆盖父组的 token 只要一个
    变量,但那会把写权限扩到整个 `rp-ai`(含技能仓和 sync 的那两个组),正好是拆开这个凭据
    要避免的事。

    `env_var` 不是装饰:凭据没配时上传者看到的是"未配置",而去配它的人需要知道**哪一个**
    变量是空的 —— 现在有两个,笼统一句"凭据未配置"会让人去改另一个。
    """

    item_type: str
    group_path: str
    token: str
    env_var: str


def target_for(item_type: str) -> UploadTarget:
    """按条目类型取上传目标。`ai-platform` 没有包可传,所以不在这里。"""
    s = get_settings()
    if item_type == "tool":
        return UploadTarget(
            item_type="tool",
            group_path=s.gitlab_user_tools_group,
            token=s.gitlab_user_tools_token,
            env_var="GITLAB_USER_TOOLS_TOKEN",
        )
    if item_type == "agent":
        return UploadTarget(
            item_type="agent",
            group_path=s.gitlab_user_agents_group,
            token=s.gitlab_user_agents_token,
            env_var="GITLAB_USER_AGENTS_TOKEN",
        )
    raise GitLabWriteError(f"{item_type} 类型的条目没有可上传的包。")


def _token(target: UploadTarget) -> str:
    if not target.token:
        raise GitLabWriteError(
            f"未配置上传 {target.item_type} 包所需的 GitLab 凭据({target.env_var}),"
            "本次上传未保存。"
        )
    return target.token


def _field_errors(body: str) -> dict[str, list[str]]:
    """把 GitLab 的校验错误体解析成 {字段: [消息]}。

    校验失败时 GitLab 答的是 ``{"message": {"name": ["has already been taken"], ...}}``。
    解析它是为了让调用方能按字段处置,而不是对一整串文本做子串匹配。

    **必须在截断之前解析。** 给人看的 `detail` 只留前 300 字符,而截断会把 JSON 切坏。
    解析不出来就返回空字典 —— 非校验类的失败(网关错误、HTML 错误页)本来就没有字段。
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return {}
    msg = data.get("message") if isinstance(data, dict) else None
    if not isinstance(msg, dict):
        return {}
    out: dict[str, list[str]] = {}
    for field, val in msg.items():
        if isinstance(val, str):
            out[str(field)] = [val]
        elif isinstance(val, list):
            out[str(field)] = [str(v) for v in val]
    return out


def _api(
    method: str, path: str, payload: dict | None = None, *, target: UploadTarget
) -> dict | list:
    """调 GitLab API。401/403 单独成一类,因为那是凭据问题而不是请求问题。"""
    s = get_settings()
    url = f"{s.gitlab_url.rstrip('/')}/api/v4/{path.lstrip('/')}"
    data = urllib.parse.urlencode(payload).encode() if payload else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"PRIVATE-TOKEN": _token(target)}
    )
    try:
        with _opener().open(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") or ""
        # 先按完整响应体解析字段,再截断给人看的那一份。顺序反了会把 JSON 切坏。
        fields = _field_errors(raw)
        detail = raw[:300]
        if exc.code in (401, 403):
            log.error("GitLab 拒绝了写请求(%s %s): HTTP %s", method, path, exc.code)
            raise CredentialExpired(
                f"上传 {target.item_type} 包用的 GitLab 凭据({target.env_var})已失效"
                "(过期或被吊销),请更新后重试。",
                status=exc.code,
            ) from exc
        if exc.code == 404 and path.startswith("groups/"):
            # 组查不到,最常见的原因不是组不存在,而是**这个 token 看不到它**:一个只覆盖
            # 另一个组的 group token 去问这个组,GitLab 答的就是 404。照原样报 "HTTP 404"
            # 会让人去查组名,而组名往往是对的。
            raise GitLabWriteError(
                f"找不到 GitLab 组 {target.group_path},或者 {target.env_var} 这个凭据"
                "没有它的访问权限(组访问令牌只能访问它所属的那一个组)。",
                status=exc.code,
            ) from exc
        log.warning("GitLab %s %s -> HTTP %s %s", method, path, exc.code, detail)
        raise GitLabWriteError(
            f"GitLab 返回 HTTP {exc.code}:{detail}", status=exc.code, errors=fields
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 网络/DNS/超时都是同一件事
        log.warning("连不上 GitLab(%s %s): %s", method, path, exc)
        raise GitLabWriteError("暂时连不上 GitLab,请稍后重试。", retryable=True) from exc


#: 组路径 → 数字 id。**按路径分键**,不是一个全局的 int:有两个上传组,而一个共用的缓存会
#: 让第一次上传解析出的 id 被另一种类型直接复用 —— tool 的项目建到 agent 组里去,门户不
#: 报错、条目也正常,只有去 GitLab 看才发现。
_namespace_ids: dict[str, int] = {}


def namespace_id(target: UploadTarget) -> int:
    """上传组的数字 id,建项目要用。

    配置里存的是**路径**(和 sync 的两个组一致,排查时看得懂),这里解析成 id 并缓存。
    组被改名路径就失效 —— 但改名这件事本来就该有人知道,而 id 静默漂移不会。
    """
    cached = _namespace_ids.get(target.group_path)
    if cached is not None:
        return cached
    path = target.group_path
    group = _api("GET", f"groups/{urllib.parse.quote(path, safe='')}", target=target)
    if not isinstance(group, dict) or "id" not in group:
        raise GitLabWriteError(f"找不到 GitLab 组 {path}")
    _namespace_ids[path] = int(group["id"])
    return _namespace_ids[path]


#: `(父组路径, 分类 slug)` → 真正要用的组路径。缓存的是**判定结果**而不是 id:回落发生在
#: 「子组不存在」这个事实上,而那件事不会在一次进程生命周期里反复变。
_group_paths: dict[tuple[str, str], str] = {}


def subgroup_target(target: UploadTarget, category_slug: str | None) -> UploadTarget:
    """把上传目标收窄到分类子组(`<父组>/<分类 slug>`);子组不存在就原样返回父组目标。

    **为什么按分类分子组。** 技能侧早就这么做了 —— 仓内路径是 `<category_slug>/<目录>`
    (`skill_storage.py`)。tool/agent 这边此前所有人的包都平铺在同一个组里,于是「这个包属于
    哪个组织」在 GitLab 上看不出来,只能回门户查。两侧用同一个手法,以后新增一个部门只要在
    GitLab 上建一个同名子组就自动生效,不用改代码。

    **子组不存在时回落父组,不是报错。** 上传是用户面对的动作,不能因为管理员还没建那个子组
    就失败 —— 那个失败对上传者毫无意义、也无从自救。回落之后包仍然进得去、条目仍然正确,只是
    在 GitLab 上少一层目录。代价是「子组建错名字」会静默地表现为「都堆在父组」,所以返回值里
    带上真实用的路径、由调用方记进日志。

    **不自动建子组。** 建组是写操作,而这个凭据的角色是给「建项目」用的;让它顺手建组会把写
    权限的用途悄悄扩大一档。缺子组由人去建,一次的事。

    token 没配时原样返回:真正那句「哪个变量没配」的消息在后面 `create_project` 那步给,
    在这里抢先抛一个措辞不同的错只会让人去查错的地方。
    """
    slug = (category_slug or "").strip().strip("/")
    if not slug or not target.token:
        return target
    key = (target.group_path, slug)
    cached = _group_paths.get(key)
    if cached is None:
        candidate = f"{target.group_path}/{slug}"
        try:
            group = _api(
                "GET",
                f"groups/{urllib.parse.quote(candidate, safe='')}",
                target=target,
            )
            found = isinstance(group, dict) and "id" in group
        except GitLabWriteError:
            # 404(子组没建)、403、以及任何读不到的情况都按「没有这个子组」处理。读不到
            # 子组的凭据也建不进去,回落父组是这两种情况下都对的答案。
            found = False
        cached = candidate if found else target.group_path
        _group_paths[key] = cached
        if cached == target.group_path:
            log.info(
                "GitLab 子组 %s/%s 不存在,包放在父组 %s 下",
                target.group_path, slug, target.group_path,
            )
    if cached == target.group_path:
        return target
    return replace(target, group_path=cached)


def create_project(
    *, slug: str, name: str, description: str | None = None, target: UploadTarget
) -> ProjectRef:
    """在上传组下建一个项目。

    **按 GitLab 指出的字段分别处置,不要一遇到冲突就把名字和路径一起改。**

    GitLab 在校验失败时会说清是哪个字段的问题(见 `GitLabWriteError.errors`)。原来这里只对
    整条错误消息做子串匹配,于是"路径被占"也会触发改名字,而改名字用的后缀是 `名字 (2)` ——
    ASCII 圆括号不在 GitLab 项目名的白名单里,于是第二轮换来一个字符集 400,而那条消息里没有
    "has already been taken",循环当场把 GitLab 的 JSON 原文抛出去。上传者看到的是"名字有非法
    字符",而他填的名字本来是合法的:那对括号是我们自己加的。实测现场:组里一个 0 提交的归档
    项目占着 `emc-agent` 这个名字,于是任何人用这个名字上传都失败。

    退路的安全性来自**构造**,不是来自我们复述 GitLab 的正则:`slug` 的字符集是 `[a-z0-9-]`,
    是 GitLab 项目名白名单的严格子集,所以它必然合法。GitLab 那条正则是 GitLab 的、会随版本
    变、还包含 emoji 区间 —— 在这里重新实现一遍只会漂。
    """
    base_path = slug
    base_name = (name or "").strip()[:255]
    # 必然合法的退路(见上)。用户没填名字时它也是第一选择。
    safe_name = base_path
    path = base_path
    display = base_name or safe_name
    last: GitLabWriteError | None = None
    for attempt in range(1, 6):
        try:
            proj = _api(
                "POST",
                "projects",
                {
                    "name": display,
                    "path": path,
                    "namespace_id": str(namespace_id(target)),
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
                target=target,
            )
        except GitLabWriteError as exc:
            # 只有 GitLab 明确指出字段问题时才值得重试。凭据、网络、5xx 一律直接抛 ——
            # 重试同一个请求不会有不同结果,而多试 5 次只是让用户多等。
            path_taken = exc.taken("path")
            name_taken = exc.taken("name", "project_namespace.name")
            name_rejected = exc.rejected("name")
            if not exc.errors and "has already been taken" in str(exc):
                # 兜底:GitLab 换了错误体的形状(或者答的不是那个 {"message": {...}} 结构),
                # 我们解析不出字段。这时**不能静默地不重试** —— 那会让"名字被占"重新变成上传
                # 失败,而且是无声的退化。
                #
                # 只推进路径:路径由 slug 派生,字符集是 [a-z0-9-],推进它不可能引入新的失败
                # 模式;而在不知道是哪个字段的情况下去动名字,恰恰是这次 bug 的成因。
                path_taken = True
            if not (path_taken or name_taken or name_rejected):
                raise
            last = exc
            # 路径被占就推进路径,这一步和名字那边互不相干 —— 放在前面是为了让"名字非法 +
            # 路径被占"同时发生时一轮就把两件事都纠正,而不是各花一轮。
            if path_taken:
                path = f"{base_path}-{attempt + 1}"
            if name_rejected:
                # 名字不合 GitLab 的规矩(用户填了括号、冒号之类)。退到必然合法的形式。
                # 已经在用那个形式还被拒,说明我们的假设错了,再试没有意义。
                if display == safe_name:
                    raise
                display = safe_name
            elif name_taken:
                # 名字**只在它自己被占时**才动 —— 路径被占跟名字没关系。序号与路径对齐,
                # 读起来一眼看得出是第几次重试。
                display = f"{safe_name}-{attempt + 1}"
            continue
        assert isinstance(proj, dict)
        return ProjectRef(
            id=int(proj["id"]),
            path_with_namespace=proj["path_with_namespace"],
            web_url=proj["web_url"],
            http_url=proj["http_url_to_repo"],
            default_branch=proj.get("default_branch") or "main",
        )
    raise GitLabWriteError(
        f"在组 {target.group_path} 下建项目连续 5 次都没成功(最后一次:名字「{display}」、"
        f"路径 {path})。**归档的项目仍然占着名字和路径** —— 组里可能有同名的归档项目(比如"
        f"上一次上传失败留下的),请管理员把它删掉或改名,或者给这个包换一个名字。",
        status=last.status if last else None,
        errors=last.errors if last else None,
    ) from last


def looks_empty(project_id: int | str, target: UploadTarget) -> bool:
    """这个项目一个提交都没有吗。

    读 `GET /projects/:id` 的 `empty_repo` 字段,**不要去数 commits**:空仓的
    `repository/commits` 在 GitLab 上会答 404(仓库还没有默认分支),于是"查不到"和"真的空"
    长得一模一样 —— 而下面那条保守规则会把 404 当成"有内容",于是最该被删的空项目恰好永远
    走不到删除分支,这个修复就白做了。`empty_repo` 是项目对象上的字段,不依赖仓库是否已初始化。

    **查不到就答 False。** 这个判断的下游是删除,所以不确定时必须偏向"当成有内容",宁可留
    一个残骸也不要删掉别人的东西。
    """
    try:
        proj = _api("GET", f"projects/{project_id}", target=target)
    except GitLabWriteError:
        return False
    return isinstance(proj, dict) and proj.get("empty_repo") is True


def delete_project(project_id: int | str, target: UploadTarget) -> None:
    """删掉一个项目。**只用于本次请求刚建、而且一个提交都没收到的那种。**

    为什么不是归档:归档既不释放 name 也不释放 path。于是一次失败的上传把这个包的名字**永久**
    占死,下一个用同名的人连项目都建不出来,而他看到的报错跟真实原因毫无关系。实测的现场就是
    `rp-ai/rp-ai-user-agents` 里一个 0 提交的归档项目占着 `emc-agent`,任何人用这个名字上传
    都失败。原来这里试图靠"先改名再归档"来腾出名字,但那个改名自己用了 `名字 (failed …)` ——
    括号非法,所以它从来没有成功过,只留下一行 warning。

    删除在这个实例上是**延迟删除**,GitLab 会立刻把项目改名(`<原名>-deletion_scheduled-<id>`),
    名字当场释放,而项目在保留期内仍可恢复。加上这个仓一个提交都没有,没有任何东西可失去。
    """
    _api("DELETE", f"projects/{project_id}", target=target)


def archive_project(project_id: int | str, target: UploadTarget) -> None:
    """归档而不是删除。

    门户里删掉一行是可逆的,删掉一个仓库不是 —— 包的历史版本、谁在什么时候改了什么,
    删完再想查就没有了。归档后它从列表里消失、变成只读,该有的都还在。
    """
    _api("POST", f"projects/{project_id}/archive", target=target)


def compare_url(web_url: str, from_ref: str, to_ref: str) -> str:
    """两个版本之间的 diff 页面。审核人要看的是"这次改了什么",不是重读整包。"""
    return f"{web_url.rstrip('/')}/-/compare/{urllib.parse.quote(from_ref)}...{urllib.parse.quote(to_ref)}"


def list_tree(
    project_id: int | str, ref: str, *, target: UploadTarget, limit: int = 500
) -> list[dict]:
    """项目在某个 ref 上的文件清单。

    审核页读的是 GitLab 上**实际存在**的东西,而不是上传时我们记下的清单。两者本该一致,
    但如果有人直接在 GitLab 上改了这个项目,审核人应该看到改后的样子 —— 他要批准的是仓库
    里的内容,不是一份历史快照。

    `target` 按条目类型给:两个上传组各有自己的 token,拿错那个问不到这个项目。
    """
    out: list[dict] = []
    page = 1
    while len(out) < limit:
        query = urllib.parse.urlencode(
            {"ref": ref, "recursive": "true", "per_page": 100, "page": page}
        )
        batch = _api(
            "GET", f"projects/{project_id}/repository/tree?{query}", target=target
        )
        if not isinstance(batch, list) or not batch:
            break
        out.extend(e for e in batch if e.get("type") == "blob")
        if len(batch) < 100:
            break
        page += 1
    return out[:limit]


# --- git -------------------------------------------------------------------


def _scrub(text: str) -> str:
    """把 token 从任何要落日志或给用户看的文本里抹掉。

    **两个都抹**,不只是这次用的那一个:`publish_tree` 的失败原文是直接给上传者看的,而
    "只抹当前那个"这种写法在加第二个凭据时静默失效 —— 漏掉的那个会原样出现在错误里。
    """
    s = get_settings()
    for token in (s.gitlab_user_agents_token, s.gitlab_user_tools_token):
        if token:
            text = text.replace(token, "***")
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


def signing_args() -> list[str]:
    """给 git 的签名参数。没配密钥就返回空 —— 也就是历史行为(不签名)。

    **为什么门户要签名:** 上传包的目标组开着 GitLab 的「Reject unsigned commits」推送规则。
    实测的失败长这样:包解开了、项目建好了,`git push` 被 pre-receive 钩子拒收
    (`Commit must be signed with a GPG key`),两个 ref 一起 rejected。

    **为什么是 SSH 签名:** 镜像里没有 gpg,有 ssh-keygen(openssh-client);而这个 GitLab
    实例的那条规则接受 SSH 签名 —— 本仓自己的提交就是这么签着推上去的。

    配置值是**公钥**路径,私钥按 `ssh-keygen -Y sign` 的约定在同一目录、同名去掉 `.pub`。
    两个文件都检查存在:少了公钥 git 直接报错,少了私钥的报错来自 ssh-keygen 且措辞离原因很
    远,而这两种情况都是部署漏挂了文件,越早说越好。
    """
    key = (get_settings().git_signing_key or "").strip()
    if not key:
        return []
    pub = Path(key)
    if not pub.is_file():
        raise GitLabWriteError(
            f"配了提交签名公钥 GIT_SIGNING_KEY={key},但容器里没有这个文件 —— "
            "部署漏挂了密钥目录。本次上传未保存。"
        )
    private = pub.with_suffix("") if pub.suffix == ".pub" else pub
    if not private.is_file():
        raise GitLabWriteError(
            f"签名要用的私钥 {private} 不在容器里。GIT_SIGNING_KEY 填的是**公钥**路径,"
            "私钥必须在同一目录、同名去掉 .pub(ssh-keygen 就是这么找它的)。本次上传未保存。"
        )
    return ["-c", "gpg.format=ssh", "-c", f"user.signingkey={pub}"]


#: pre-receive 钩子拒收时,GitLab 原文里的这句话说明是签名规则。
_UNSIGNED_REJECTION = "must be signed"


def _push_rejection_hint(detail: str, *, signed: bool) -> str | None:
    """把推送规则的拒收翻译成"谁去改什么"。认不出来就返回 None。

    上传者看到的是这句话,而 git 的原文对他没有任何可操作性 —— 他既不是那个能改推送规则的
    人,也无法从"pre-receive hook declined"看出这跟他的包无关。
    """
    if _UNSIGNED_REJECTION not in detail.lower():
        return None
    if not signed:
        return (
            "目标项目开着 GitLab 的「必须签名提交」推送规则,而门户没有配签名密钥"
            "(GIT_SIGNING_KEY),所以这次推送被拒了。**这不是你的包的问题** —— 包已经解开、"
            "项目也建好了。请管理员给门户配一个 SSH 签名密钥,或者改掉新建项目的默认推送规则。"
        )
    return (
        "门户的提交**已经签名**,GitLab 仍然拒收 —— 说明这条推送规则要求签名可归属到推送者"
        "本人,而门户是用组访问令牌的机器身份推的,签名密钥不属于它。这条只能由管理员改掉"
        "新建项目的默认推送规则(Admin Area → Push Rules),门户侧没有别的解法。"
    )


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
    target: UploadTarget,
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
        f"https://oauth2:{urllib.parse.quote(_token(target), safe='')}@{host}\n",
        encoding="utf-8",
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
    # 签名参数和身份一起传:两者都是逐条 -c,不碰全局 git config,也不碰工作区的 .git/config
    # (那里会落到磁盘上)。没配密钥时 sign 是空列表,行为与从前一字不差。
    sign = signing_args()
    signed = bool(sign)
    _git(work, *ident, *sign, "commit", "-q", "--allow-empty",
         *(["-S"] if signed else []), "-m", message)
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()

    tag = f"v{version}"
    # tag 也签。推送规则拒收时两个 ref 是一起被拒的,而一个指向已签名提交的未签名 tag 是否
    # 满足规则我们没有依据 —— 签它的代价是零,赌错的代价是同一个失败再来一遍。
    _git(work, *ident, *sign, "tag", "-s" if signed else "-a", tag, "-m", message)
    # 一次 push 送出分支和这个 tag。分支用显式 refspec:空项目里没有上游可依赖。
    try:
        _git(work, "push", project.http_url, f"HEAD:refs/heads/{branch}",
             f"refs/tags/{tag}", cred_file=cred)
    except GitLabWriteError as exc:
        hint = _push_rejection_hint(exc.detail, signed=signed)
        if hint is None:
            raise
        # 原文留在后面:上传者需要那句人话,而排查的人需要 GitLab 到底说了什么。
        raise PushRuleRejected(f"{hint}\n\nGitLab 原文:{exc.detail}") from exc
    return sha, tag
