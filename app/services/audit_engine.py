"""调用审计引擎。

引擎在另一个仓(`rp-ai-miao/skill-audit`),边界与契约见 `.kiro/specs/skill-audit/service-split.md`。
这个模块是门户这一侧的全部接触面:除它以外,门户任何地方都不该知道引擎的存在。

三件事值得先说清楚,因为它们决定了这里为什么长这样:

**只走事件流。** 引擎只有一种表述:`stage` 事件、每 15 秒一个 `ping`、最后一个 `result` 或 `error`。
即使调用方不关心进度也走它 —— `ping` 是"引擎还活着"的唯一证据。没有它,读超时就必须大于一次审计的
最坏情况(扫描 900 秒 + 判定 200 秒),于是引擎挂死要等将近二十分钟才暴露;有它,读超时压到一分钟,
而总时长仍然可以很长。实测:一次 89 秒的审计里有 63.6 秒静默,期间准时来了 5 个 ping。

**用 urllib,不引新依赖。** 门户调 GitLab 一直用 `urllib.request`。它的 `timeout` 是**每次 socket
操作**的超时,不是总时长,正好是我们要的语义。

**失败不抛,返回一个说明失败的结论。** 上层(`gate`)要的是"这次审计能不能作为写入依据",而不是异常。
引擎不可达时返回 `state=suspect`(fail closed)+ `coverage=none`(什么都没查)+ `retryable=True`
(再试可能就好),`gate` 据此得到:新提交转人工复核,而已上线内容拒绝改动并要求重试。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable

from app.config import get_settings

log = logging.getLogger(__name__)

#: 门户认得的契约版本。引擎报别的版本就当它不可用 —— 猜字段比停下来更危险。
API_VERSION = "v1"

#: 覆盖度的三个取值。它们是响应结构的一部分,也是门户自己库里的列值,所以不从引擎拉取:改动它们是
#: 破坏性契约变更,会撞上 API_VERSION 校验,而不是静默漂移。
COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
COVERAGE_NONE = "none"

#: 门户自己造的 cause:引擎那边根本没应答,所以不可能由引擎给。
CAUSE_ENGINE_UNREACHABLE = "engine_unreachable"
CAUSE_ENGINE_CONTRACT = "engine_contract_mismatch"

_VOCAB_TTL_SECONDS = 600


class BundleTooLarge(Exception):
    """附件大到不值得解包。

    这是**输入**问题,作者自己能改,所以照旧由 `main.py` 的处理器答成 422 —— 措辞用引擎给的那句,
    因为"多大算大"是引擎的策略。
    """


@dataclass
class Finding:
    """一条发现。字段名就是契约,`gate.record` 按名存库。"""

    source: str = ""
    category: str = ""
    severity: str = ""
    evidence: str | None = None
    detail: str | None = None
    file: str | None = None
    line: int | None = None
    rule_id: str = ""
    aitech: str | None = None
    remediation: str | None = None


@dataclass
class AuditResult:
    """一次审计的结论,门户侧的镜像。

    不共享包:两个仓各自发布,共享包意味着两边的版本必须同时升。改名或删字段要升 `API_VERSION`,
    新增字段是兼容的(这里用"只取认识的字段"来实现)。
    """

    state: str = "suspect"
    verdict: str | None = None
    raw: str | None = None
    findings: list[Finding] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    reason: str = ""
    coverage: str = COVERAGE_COMPLETE
    cause: str | None = None
    #: 再试一次有可能得到不同答案 —— 引擎报的事实,不是门户的推断。gate 用它决定要不要拒绝改动
    #: 已上线内容并让人重试。
    retryable: bool = False
    content_sha: str | None = None
    judge_model: str | None = None
    scanner_version: str | None = None
    policy_fingerprint: str | None = None
    analyzers_used: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)

    @property
    def is_danger(self) -> bool:
        return self.state == "danger"


def content_key(name: str, skill_md: str, artifact_bytes: bytes | None) -> str:
    """这次审计对应的内容键,门户自己的。

    它只用来在门户库里找"同样内容的近期判定"(`gate.recent_audit`),从不与引擎比对 —— 所以两边不需要
    共用哈希算法。曾经设计成"门户把键传给引擎、引擎原样回显",撤回了:那是让引擎替调用方保管状态,
    凭空造出一个必须对齐的东西。
    """
    h = hashlib.sha256()
    h.update(name.encode("utf-8"))
    h.update(b"\x00")
    h.update(skill_md.encode("utf-8"))
    if artifact_bytes:
        h.update(b"\x00")
        h.update(hashlib.sha256(artifact_bytes).digest())
    return h.hexdigest()


def _unreachable(detail: str, cause: str = CAUSE_ENGINE_UNREACHABLE) -> AuditResult:
    """审计没能建立时的结论。

    `suspect` 而不是 `danger`:我们并没有发现问题,只是没能查。`coverage=none` 让复核人看到"这份内容
    一点都没被检查过"。`retryable=True` 是因为引擎不可达是平台侧的缺口 —— 再试一次可能就好了,而这正是
    `gate` 用来保护已上线内容的依据。
    """
    return AuditResult(state="suspect", coverage=COVERAGE_NONE, cause=cause,
                       retryable=True, reason=detail)


def _multipart(fields: dict[str, str], artifact: tuple[str, bytes] | None) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                f"{v}\r\n").encode("utf-8")
    if artifact is not None:
        filename, blob = artifact
        # 文件名里的引号与换行会破坏分隔结构 —— 引号转义,换行直接去掉。
        safe = filename.replace("\\", "_").replace("\"", "'").replace("\r", "").replace("\n", "")
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"artifact\"; "
                f"filename=\"{safe}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode("utf-8")
        out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _finding_from(d: dict) -> Finding:
    known = {f for f in Finding.__dataclass_fields__}
    return Finding(**{k: v for k, v in d.items() if k in known})


def _result_from(d: dict) -> AuditResult:
    """按字段名取,只取认识的。

    引擎新增字段不该让门户报错;而**缺**字段要看得见 —— 少了 `state` 说明这不是一个结论,当不可用处理,
    而不是让 dataclass 的默认值把它伪装成一次成功的审计。
    """
    if d.get("apiVersion") != API_VERSION:
        raise ValueError(f"引擎契约版本是 {d.get('apiVersion')!r},门户认的是 {API_VERSION!r}")
    if not d.get("state"):
        raise ValueError("引擎返回里没有 state,这不是一个结论")
    known = {f for f in AuditResult.__dataclass_fields__}
    kw = {k: v for k, v in d.items() if k in known and k != "findings"}
    kw["findings"] = [_finding_from(f) for f in d.get("findings") or []]
    return AuditResult(**kw)


def audit_skill(
    *,
    name: str,
    skill_md: str,
    artifact_bytes: bytes | None = None,
    artifact_filename: str | None = None,
    uploader_eid: str | None = None,
    use_cache: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> AuditResult:
    """跑一次审计。阻塞到有结论,失败也返回结论(见模块开头)。

    名字与拆分之前那个进程内函数相同,而且参数一一对应。这不是偷懒:上传、编辑、流式三条路径的测试
    (12 个文件)打的就是这个桩,名字与签名不变意味着**那些测试不用改一行,就能真正验证新的接线**。

    `on_progress` 拿到的是引擎转发的阶段名,与进程内那版一字不差 —— 上传界面的阶段条不用改。
    """
    s = get_settings()
    body, content_type = _multipart(
        {"name": name, "skill_md": skill_md,
         "uploader_eid": uploader_eid or "",
         "use_cache": "true" if use_cache else "false"},
        (artifact_filename or "artifact.bin", artifact_bytes) if artifact_bytes else None,
    )
    req = urllib.request.Request(
        f"{s.audit_engine_url.rstrip('/')}/v1/audits", data=body, method="POST",
        headers={"Authorization": f"Bearer {s.audit_engine_token}",
                 "Accept": "text/event-stream", "Content-Type": content_type},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=s.audit_engine_read_timeout)
    except urllib.error.HTTPError as exc:
        detail = (exc.read().decode("utf-8", "replace") or "")[:300]
        log.warning("审计引擎拒绝了请求: HTTP %s %s", exc.code, detail)
        return _unreachable(f"审计服务返回 HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 - 网络、超时、DNS 都是同一件事:没拿到结论
        log.warning("连不上审计引擎: %s", exc)
        return _unreachable("审计服务暂时连不上")

    event: str | None = None
    result: AuditResult | None = None
    failure: AuditResult | None = None
    too_big: str | None = None
    try:
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event: "):
                    event = line[7:]
                    continue
                if not line.startswith("data: ") or event is None:
                    continue
                payload = json.loads(line[6:])
                if event == "stage":
                    if on_progress and payload.get("stage"):
                        on_progress(payload["stage"])
                elif event == "error":
                    # 附件过大是作者能改的输入问题,别的都是平台的。
                    if payload.get("cause") == "bundle_too_big":
                        too_big = payload.get("detail") or "附件过大"
                    else:
                        failure = _unreachable(payload.get("detail") or "审计未能完成")
                elif event == "result":
                    result = _result_from(payload)
    except ValueError as exc:                      # 契约不对:停下来,不要猜字段
        log.error("审计引擎的返回不符合契约: %s", exc)
        return _unreachable(str(exc), cause=CAUSE_ENGINE_CONTRACT)
    except Exception as exc:  # noqa: BLE001 - 读流中断
        log.warning("审计事件流中断: %s", exc)
        return _unreachable("审计过程中连接中断")

    if too_big is not None:
        raise BundleTooLarge(too_big)
    if failure is not None:
        return failure
    if result is None:
        # 流结束了却没有终结事件 —— 与"引擎说它失败了"不同,但对调用方是同一件事:没有结论。
        return _unreachable("审计没有给出结论")
    return result


# --------------------------------------------------------------------------- 分类表

_vocab: dict | None = None
_vocab_at: float = 0.0


def _vocabulary() -> dict | None:
    """引擎发布的分类表,缓存 10 分钟。取不到返回 None(调用方要说自己算不出来)。"""
    global _vocab, _vocab_at
    if _vocab is not None and time.monotonic() - _vocab_at < _VOCAB_TTL_SECONDS:
        return _vocab
    s = get_settings()
    req = urllib.request.Request(
        f"{s.audit_engine_url.rstrip('/')}/v1/vocabulary",
        headers={"Authorization": f"Bearer {s.audit_engine_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=s.audit_engine_read_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("apiVersion") != API_VERSION:
            raise ValueError(f"分类表版本 {data.get('apiVersion')!r}")
        _vocab, _vocab_at = data, time.monotonic()
        return _vocab
    except Exception as exc:  # noqa: BLE001
        log.warning("取不到审计分类表: %s", exc)
        return _vocab          # 宁可用过期的,也比算不出来好;从来没取到过时是 None


#: 归属的展示措辞。分类归引擎(author/platform/shared 是它给的标识符),这三个词归门户。
OWNER_WORDS = {"author": "作者", "platform": "平台", "shared": "作者与平台"}


#: 门户自己造的 cause 的归属。这**不是**引擎那张表的副本 —— 引擎压根没见过这些情况(它没应答,
#: 或者它的应答门户读不懂),所以只能由门户自己判定。两者都是平台的问题:调用不通、契约对不上,
#: 都不是上传者能做什么的事。
_OWN_CAUSE_OWNER = {
    CAUSE_ENGINE_UNREACHABLE: "platform",
    CAUSE_ENGINE_CONTRACT: "platform",
}


def cause_owner(cause: str | None) -> str | None:
    """谁能补上这个覆盖缺口,已渲染成中文。算不出来时返回 None。

    分类表来自引擎(`GET /v1/vocabulary`),门户不留副本 —— 归属是 cause 的纯函数,两份可能互相矛盾的
    定义分散在两个仓里,漂移时没有任何一处会报错。取不到表时返回 None,调用方必须**说自己算不出来**,
    不能报一个错的数字。

    例外是门户自己造的那两个 cause:引擎没见过它们,查它的表永远查不到。而"引擎连不上"恰好是引擎故障
    时最常出现的那一条 —— 如果它显示成"没有对应说明",复核人看到的信息比我们知道的更少。
    """
    if not cause:
        return None
    own = _OWN_CAUSE_OWNER.get(cause)
    if own:
        return OWNER_WORDS.get(own)
    vocab = _vocabulary()
    if not vocab:
        return None
    return OWNER_WORDS.get((vocab.get("causeOwners") or {}).get(cause))


def platform_owned(causes: list[str]) -> int | None:
    """这些 cause 里有多少是平台自己的。分类表取不到时返回 None,别猜。"""
    vocab = _vocabulary()
    if not vocab:
        return None
    owners = {**(vocab.get("causeOwners") or {}), **_OWN_CAUSE_OWNER}
    return sum(1 for c in causes if owners.get(c) == "platform")
