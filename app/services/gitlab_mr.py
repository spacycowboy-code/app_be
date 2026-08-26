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
from app.services import audit_engine
from app.services.audit_engine import Finding

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


# One sentence per cause, for the reviewer. Keyed on the cause the verdict layer decided, not
# on substrings of its prose: the structure is known where the decision is made, and recovering
# it by grepping a sentence breaks as soon as the wording changes — including the scanner's
# wording, which is not ours to fix. ``audit_engine.cause_owner`` says who; this says what to do.
#
# 键是引擎给的 cause 标识符,字面写在这里。它是开放集合(引擎会增加),所以这张表可能缺项 —— 缺项时
# 下面会打印"这个原因还没有对应的处置说明",看得见,而不是静默少一段。
_CAUSE_ADVICE: dict[str, str] = {
    "bundle_unloadable": (
        "扫描器无法加载这个 skill,所以**它的内容一次都没有被检查过**。原因见上面的判定理由;"
        "最常见的是 `SKILL.md` 的 frontmatter 没能解析成键值对——`name:foo` 这样冒号后面少一个空格,"
        "YAML 就会把整段读成一行字符串,于是 `name` 被当成没写,尽管肉眼看它写了。正确写法是 "
        "`name: 名字` 与 `description: 一句话`,两个字段都必填。请作者改好后重传;"
        "在那之前合入等于上架一个未经检查的 skill。"
    ),
    "bundle_unreadable_member": (
        "包里有个会运行的文件读不出来(常见于 `.pyz`、`.exe`,或非 UTF-8 编码的脚本),"
        "其余内容都已检查。请人工打开那个文件确认它做什么;或让作者把脚本平铺成可读的文本。"
    ),
    "bundle_too_big": (
        "内容超出了送给语义层的预算,超出的部分没有被语义分析看过(确定性层仍然扫了每个文件)。"
        "如果这个包确实很大,请作者拆分或去掉不必要的附件;如果它并不大,那是预算配得太紧,属平台侧。"
    ),
    "scanner_timeout": (
        "审计超时,不是查出了问题。请维护者看一眼判定器与扫描器的负载,然后重跑;"
        "不要凭这个结论决定合不合。"
    ),
    "scanner_broken": (
        "扫描器没有交出可用的报告(跑不起来、无输出,或输出不是有效 JSON)。这是我们的故障,"
        "不是作者的问题;skill 的内容没有被检查过。"
    ),
    # 门户自己造的两个:引擎没应答,或者它的应答门户读不懂。引擎故障时最常出现的就是第一个,
    # 而它原来落到"没有对应的处置说明"那句兜底上 —— 复核人看到的信息比我们知道的更少。
    "engine_unreachable": (
        "审计服务当时连不上,所以这份内容一点都没有被检查过。这不是内容的问题:"
        "**请不要凭这份 MR 判断它安全**,等审计服务恢复后让作者重新提交一次,由审计给出结论。"
    ),
    "engine_contract_mismatch": (
        "审计服务返回了门户读不懂的格式,通常是两边版本不匹配。同样什么都没检查:"
        "**不要凭这份 MR 判断内容安全**,先让维护者对齐两边版本,再让作者重提。"
    ),
    "layer_failed": (
        "语义分析或仲裁这一层没跑成,所以这次只有确定性层的结果。这是我们的故障,不是作者的问题。"
        "请维护者查判定器,再重跑一次审计。"
    ),
}


def _coverage_note(coverage: str | None, cause: str | None) -> list[str]:
    """What a reviewer needs before anything else: was this content examined?

    ``complete`` needs no note — the findings table is the whole story. Anything else means the
    table is not, and merging publishes something that was not fully looked at.
    """
    if not coverage or coverage == audit_engine.COVERAGE_COMPLETE:
        return []
    if coverage == audit_engine.COVERAGE_NONE:
        examined = "**一点都没有被检查过**"
    elif coverage == audit_engine.COVERAGE_PARTIAL:
        examined = "**只检查了一部分**"
    else:
        # 认不出来的取值不能猜成"只检查了一部分":那会把"完全没查"说轻。覆盖度是随契约版本走的封闭
        # 枚举,所以出现未知值意味着两边版本对不上,该让人看见。
        examined = f"**覆盖情况无法判断**(引擎报的是 `{coverage}`,门户不认识这个取值)"
    lines = [f"**覆盖度:** 这个 skill {examined},所以合入前请自己看一遍 diff。", ""]
    owner = audit_engine.cause_owner(cause)
    advice = _CAUSE_ADVICE.get(cause or "")
    if owner and advice:
        lines += [f"**归属:** {owner}", "", advice, ""]
    else:
        lines += ["这个原因还没有对应的处置说明,请把上面的判定理由发给维护者。", ""]
    return lines


def _findings_markdown(skill_name: str, findings: list[Finding],
                       uploader: str | None = None, reason: str | None = None,
                       coverage: str | None = None, cause: str | None = None) -> str:
    """The review MR's body: why this is here, who to talk to, and what was found.

    The reason line is not decoration. A verdict of "suspect" does not always come with
    findings — "a layer did not finish" is the commonest one, and it produces none. Rendering
    only the findings table left those MRs as a bare table header: the reviewer could not tell
    why the skill was in front of them, and the author never learned what to fix. Seen in
    production on 2026-08-19, twice for the same skill, and merged with no evidence.
    """
    lines = [f"### 🔒 安全审计:`{skill_name}` 判为**可疑**,请复核后决定是否合入", ""]
    # Who uploaded it, stated rather than left to be inferred from the folder name
    # in the diff. A reviewer's first question is who to talk to.
    if uploader:
        lines += [f"**上传者:** {uploader}", ""]
    if reason:
        lines += [f"**判定理由:** {reason}", ""]

    lines += _coverage_note(coverage, cause)

    if findings:
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
    elif coverage == audit_engine.COVERAGE_COMPLETE:
        # Fully examined and nothing gating: this should not have become a review at all.
        lines += ["没有命中,且内容已全部检查过。这条复核单不该存在,请把参考编号发给维护者。", ""]
    lines.append("")
    lines.append("_合入此 MR = 该 skill 上线。_")
    return "\n".join(lines)


class ReviewRequestFailed(RuntimeError):
    """复核 MR 没开出来。

    不能当成"最好能开就开"的小事:内容已经写在复核分支上,而数据库那一行是 pending。没有 MR 就没有人
    被通知、没有东西可复核,而收敛器靠 MR 状态推进 —— 那一行会永远停在"等待复核",上传者看到的是假的。

    按"每个投影都要有一条真能跑的重算路径"这条判据,"复核 MR 存在"是投影(能从复核分支 + 审计 ref +
    pending 状态算出来)。在那条重算路径做出来之前,这里选择让不变量成立:**内容在复核分支上 ⇒ 一定有
    MR**。开不出来就当这次发布没成功,由调用方删掉分支并让请求失败。
    """


def review_suspect(
    *, source_branch: str, target_branch: str, skill_name: str,
    findings: list[Finding], audit_ref: str, uploader: str | None = None,
    reason: str | None = None, coverage: str | None = None, cause: str | None = None,
) -> str | None:
    """Create a Draft MR from source_branch and post findings. Returns MR url."""
    settings = get_settings()
    if not settings.audit_review_enabled:
        return None
    try:
        body = _findings_markdown(skill_name, findings, uploader, reason,
                                  coverage, cause)
        payload = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": (f"Draft: [skill-audit] 可疑复核 {skill_name}"
                      + (f" — {uploader}" if uploader else "")
                      + f" ({audit_ref})"),
            "description": body,
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
                         {"body": body})
            except Exception as exc:  # note is best-effort
                log.warning("MR note failed: %s", exc)
        log.info("opened Draft MR for suspect skill '%s': %s", skill_name, mr_url)
        return mr_url
    except Exception as exc:
        # 不再吞掉。原来这里返回 None,于是内容留在复核分支上而没有 MR,那一行永远停在 pending。
        log.warning("Draft MR creation failed for '%s': %s", skill_name, exc)
        raise ReviewRequestFailed(str(exc)) from exc


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
