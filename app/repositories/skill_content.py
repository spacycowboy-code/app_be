"""技能内容:按不可变引用读。

调用方只说"要**哪一版**的**哪个路径**";字节从哪来是这个模块的事。

* `ref` 是**不可变引用**(今天是 commit sha)。同一个 ref 永远对应同一批字节 —— 所以缓存可以是
  容器本地的私有目录,不需要失效逻辑,也不可能让两个副本读到不一致的内容。
* **读不到就抛 `ContentUnavailable`,不回落到数据库里的 `instructions`。** 那是第二份事实,
  回落等于拿可能过期的内容去编辑、审计或下载。
* 缓存目录**不能是 bind mount**,否则又把副本钉回一台机器。

GitLab 是这个内容存储今天的实现。厂商概念(project、`PRIVATE-TOKEN`、tree 分页)不出这个模块。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

#: 归档是按需生成的:实测最大技能首次 10.84 秒、紧接着再取 2.08 秒。超时按首次那一档设。
_TIMEOUT = 30

_PER_PAGE = 100
#: 100 页 = 10000 个文件。超过这个数不是技能,是代码仓。
_MAX_TREE_PAGES = 100


class ContentUnavailable(RuntimeError):
    """引用或路径取不到内容。让调用方明确失败,而不是悄悄换一份内容。"""


class NotFound(ContentUnavailable):
    """这个引用下没有这个路径。

    和"上游故障"必须分开:收敛器要靠这个区分"技能在仓库里被删了"(把 `status` 置成 `archived`)和
    "GitLab 挂了"(什么都别做)。合成一个异常的话,GitLab 挂一次就会把所有技能归档。
    端到端验收时发现的:删空一个目录之后列它,接口返回 404。
    """


class Conflict(RuntimeError):
    """有人在你读到之后改了同一个技能的文件。**不要重试** —— 重试等于拿自己这份盖掉对方。"""


@dataclass
class _Stats:
    hits: int = 0
    misses: int = 0


#: 缓存命中率要能看见 —— 没有这个数,缓存悄悄失效了没人会发现。
STATS = _Stats()


def _opener() -> urllib.request.OpenerDirector:
    settings = get_settings()
    handlers: list = [urllib.request.ProxyHandler({})]  # 内网主机,绕开代理
    if not settings.gitlab_verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _fetch(path_and_query: str) -> bytes:
    """向内容存储取一次字节。测试把这个函数换掉,不需要网络。"""
    settings = get_settings()
    project = urllib.parse.quote(str(settings.audit_mr_project), safe="")
    url = (f"{settings.gitlab_url.rstrip('/')}/api/v4/projects/{project}/repository/"
           f"{path_and_query.lstrip('/')}")
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": settings.gitlab_token})
    try:
        with _opener().open(req, timeout=_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotFound(f"404 {path_and_query}") from exc
        raise ContentUnavailable(f"{exc.code} {path_and_query}") from exc
    except Exception as exc:  # 网络、超时、TLS
        raise ContentUnavailable(f"{type(exc).__name__} {path_and_query}") from exc


def _cache_root() -> Path:
    return Path(get_settings().content_cache_dir)


def _cached(ref: str, key: str, produce) -> bytes:
    """按(ref, key)缓存。内容不可变,所以命中就直接用,没有失效判断。"""
    if not ref:
        raise ContentUnavailable("没有引用(ref 为空),不猜内容在哪一版")
    slot = _cache_root() / ref[:40] / hashlib.sha256(key.encode()).hexdigest()[:32]
    if slot.is_file():
        STATS.hits += 1
        return slot.read_bytes()
    data = produce()
    STATS.misses += 1
    try:
        slot.parent.mkdir(parents=True, exist_ok=True)
        tmp = slot.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(slot)  # 原子换名,避免读到写一半的文件
        _evict()
    except OSError as exc:  # 缓存写不进去不该影响这次请求
        log.warning("content cache write failed: %s", exc)
    return data


def _evict() -> None:
    """超过上限就按最久未用删。缓存是可丢弃的,删错了下次重取。"""
    cap = get_settings().content_cache_max_mb * 1024 * 1024
    files = [p for p in _cache_root().rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    if total <= cap:
        return
    for p in sorted(files, key=lambda p: p.stat().st_mtime):
        try:
            total -= p.stat().st_size
            p.unlink()
        except OSError:
            continue
        if total <= cap:
            return


def read(ref: str, path: str) -> bytes:
    """取某一版里某个文件的字节。"""
    def produce() -> bytes:
        return _fetch(f"files/{urllib.parse.quote(path, safe='')}/raw"
                      f"?ref={urllib.parse.quote(ref, safe='')}")
    return _cached(ref, f"file:{path}", produce)


def read_text(ref: str, path: str) -> str:
    return read(ref, path).decode("utf-8", "replace")


def list_files(ref: str, prefix: str) -> list[str]:
    """列出某一版里某个目录下的全部文件路径(递归,不含目录本身)。"""
    def produce() -> bytes:
        out: list[str] = []
        for page in range(1, _MAX_TREE_PAGES + 1):
            raw = _fetch(f"tree?ref={urllib.parse.quote(ref, safe='')}"
                         f"&path={urllib.parse.quote(prefix, safe='')}"
                         f"&recursive=true&per_page={_PER_PAGE}&page={page}")
            batch = json.loads(raw.decode("utf-8"))
            out += [e["path"] for e in batch if e.get("type") == "blob"]
            if len(batch) < _PER_PAGE:
                return json.dumps(out).encode()
        # 页数上限是防线,不是分页逻辑:上游若一直返回满页(它坏了,或者这个
        # "技能"其实是个代码仓),不设上限会把内存吃光。实测最大的技能 63 个文件。
        raise ContentUnavailable(
            f"{prefix} 下的文件超过 {_MAX_TREE_PAGES * _PER_PAGE} 个,不像一个技能")
    return json.loads(_cached(ref, f"tree:{prefix}", produce).decode())


def _strip_wrapper(names: list[str], prefix: str) -> dict[str, str]:
    """把归档里的路径换算成"相对技能目录"。

    归档带两层前缀:GitLab 自己的包装目录(`<项目>-<ref>-<ref>-<路径>/`),以及技能在仓库里的
    完整路径。实测一条:
        rp-ai-skill-<sha>-<sha>-radio-design-1-isolation…/radio-design-1/isolation…/SKILL.md
    调用方要的是 `SKILL.md`,所以两层都要剥掉。
    """
    out: dict[str, str] = {}
    for name in names:
        if name.endswith("/"):
            continue
        rest = name.split("/", 1)[1] if "/" in name else name  # 剥 GitLab 的包装目录
        marker = prefix.strip("/") + "/"
        if marker in rest:
            rest = rest.split(marker, 1)[1]
        if rest:
            out[name] = rest
    return out


def head_ref(branch: str | None = None) -> str:
    """默认分支当前的引用。

    导入与对账要从"现在最新的那一版"开始,而这个问题过去是靠 fetch + merge 把一棵共享工作副本
    拉到最新来回答的 —— 那次 merge 会移动整棵树的 HEAD,把并发写请求脚下的分支抽走。

    与 `gitlab_sync.fetch_head_commit_sha` 的区别:那个服务任意市场项目、返回 12 位短 sha、
    失败给 None(它只是省一次重装)。这里要的是能当读引用用的完整标识,取不到必须报错。
    """
    ref = branch or get_settings().skill_repo_branch
    raw = _fetch(f"branches/{urllib.parse.quote(ref, safe='')}")
    sha = (json.loads(raw.decode("utf-8")).get("commit") or {}).get("id")
    if not sha:
        raise ContentUnavailable(f"取不到 {ref} 的当前引用")
    return str(sha)


def version_of(prefix: str, at: str) -> str | None:
    """这个子树在 `at` 这一版里的**内容身份**。

    调用方要的不是"某个提交",是"只在这个技能自己变了的时候才变的标识" —— 用整仓 HEAD 当这个标识,
    会让任何一次无关提交都把每个技能标记成有更新。今天 git 用"最后碰过这个路径的提交"来回答;
    换成产物存储,答案是那个包的 digest。所以这个函数返回的是不透明标识,调用方不该解析它。

    取不到返回 None,由调用方决定退到什么。
    """
    raw = _fetch(f"commits?path={urllib.parse.quote(prefix, safe='')}"
                 f"&ref_name={urllib.parse.quote(at, safe='')}&per_page=1")
    commits = json.loads(raw.decode("utf-8"))
    return str(commits[0]["id"]) if commits else None


def _post(path: str, payload: dict) -> dict:
    """向内容存储写一次。测试把这个函数换掉,不需要网络。"""
    settings = get_settings()
    project = urllib.parse.quote(str(settings.audit_mr_project), safe="")
    url = (f"{settings.gitlab_url.rstrip('/')}/api/v4/projects/{project}/repository/"
           f"{path.lstrip('/')}")
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"PRIVATE-TOKEN": settings.gitlab_token,
                 "Content-Type": "application/json"})
    try:
        with _opener().open(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if exc.code == 400 and "has changed since" in body:
            raise Conflict(body) from exc
        raise ContentUnavailable(f"{exc.code} {body[:200]}") from exc
    except Exception as exc:
        raise ContentUnavailable(f"{type(exc).__name__} {path}") from exc


def publish(*, prefix: str, files: dict[str, bytes], expect: str | None,
            branch: str, message: str, start_branch: str | None = None) -> str:
    """把 `prefix` 这个目录的内容整体换成 `files`,返回新的引用。

    * `files` 是**这个目录应有的全部内容**(键是相对 `prefix` 的路径)。没出现的文件会被删掉 ——
      声明目标状态而不是描述增量,所以同样的输入重复执行结果一样。
    * `expect` 是调用方读到这份内容时的引用。**每个已存在的文件都带上它做前置条件**:实测
      `last_commit_id` 问的是"这个文件在这个提交之后被改过吗",按文件比而不是按分支比 ——
      无关提交把分支头推走不会造成冲突。不带这个参数是静默覆盖。
    * `expect=None` 表示这是新目录。
    * **内容没变就不提交**,返回原引用。接口本身不幂等(相同内容也会产生空提交),这道闸在这里。
    * 冲突抛 `Conflict`,**不重试**:按文件比的前置条件失败只可能是同一个技能被并发改了,
      重试等于拿自己这份盖掉对方。

    原子性:今天一次 Commits 调用把这个技能这一版的全部动作放在一起,git 保证要么全成要么全不成。
    抽象不依赖这一项 —— 对象存储没有跨对象事务,那里的实现是先写内容对象再带前置条件切 manifest。
    """
    current = read_tree(expect, prefix) if expect else {}
    actions: list[dict] = []
    for rel in sorted(files):
        full = f"{prefix.strip('/')}/{rel}"
        if rel not in current:
            actions.append({"action": "create", "file_path": full,
                            "content": base64.b64encode(files[rel]).decode(),
                            "encoding": "base64"})
        elif current[rel] != files[rel]:
            actions.append({"action": "update", "file_path": full,
                            "content": base64.b64encode(files[rel]).decode(),
                            "encoding": "base64", "last_commit_id": expect})
    for rel in sorted(current):
        if rel not in files:
            actions.append({"action": "delete",
                            "file_path": f"{prefix.strip('/')}/{rel}",
                            "last_commit_id": expect})
    if not actions:
        return expect  # type: ignore[return-value]

    payload: dict = {"branch": branch, "commit_message": message, "actions": actions}
    if start_branch:
        # 分支不存在时由这次调用顺带切出来,基点是远端的那个分支。原来的做法是本地
        # fetch + checkout -B,而本地 ref 会漂:从漂了的 ref 切出来的分支,diff 里带着
        # "把这期间合入的东西全revert 掉"。
        payload["start_branch"] = start_branch
    new_ref = _post("commits", payload).get("id")
    if not new_ref:
        raise ContentUnavailable("提交成功但响应里没有引用")
    return str(new_ref)


def tag(*, name: str, ref: str, message: str) -> bool:
    """给某一版起个名字。已存在或失败返回 False —— 名字是方便人看的标记,不是上架的依据。"""
    try:
        _post("tags", {"tag_name": name, "ref": ref, "message": message})
        return True
    except (ContentUnavailable, Conflict):
        return False


def read_tree(ref: str, prefix: str) -> dict[str, bytes]:
    """取某一版里某个目录下的全部文件:{相对路径: 字节}。

    一次归档请求拿回整棵子树。逐文件读会慢一个数量级 —— 官方技能有 63 个文件,单文件 0.5 秒。
    zip 是这个实现的传输格式,不出这个模块:调用方拿到的是路径到字节的映射,自己决定怎么打包给用户。
    """
    def produce() -> bytes:
        return _fetch(f"archive.zip?sha={urllib.parse.quote(ref, safe='')}"
                      f"&path={urllib.parse.quote(prefix, safe='')}")
    blob = _cached(ref, f"tree-bytes:{prefix}", produce)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            mapping = _strip_wrapper(zf.namelist(), prefix)
            return {rel: zf.read(name) for name, rel in mapping.items()}
    except zipfile.BadZipFile as exc:
        raise ContentUnavailable(f"{prefix} 的归档不是有效的 zip") from exc
