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

import hashlib
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
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


def archive(ref: str, prefix: str) -> bytes:
    """取某一版里某个目录的 tar.gz。整目录一次拿回,逐文件读会慢一个数量级。"""
    def produce() -> bytes:
        return _fetch(f"archive.tar.gz?sha={urllib.parse.quote(ref, safe='')}"
                      f"&path={urllib.parse.quote(prefix, safe='')}")
    return _cached(ref, f"archive:{prefix}", produce)
