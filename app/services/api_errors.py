"""接口报错的记录与诊断。

两半，刻意分开：

* ``record()`` 在请求失败的那一刻把事实写下来。它必须极其保守 —— 记录失败绝不能改变
  用户看到的响应。所有异常在这里被吞掉并只落日志。
* ``diagnose_pending()`` 事后（在 reconciler 的循环里）把还没诊断的行送给判定器。它慢、
  依赖外部服务、可能失败，所以绝不能长在请求路径上。

**为什么记录侧不顺手调 LLM**：一次判定器调用实测约 7 秒、历史上出现过 200 秒级慢响应。
挂在请求路径上等于把一次 500 变成一次超时，而且用户已经走了、没人在等那个解释。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import traceback as tb_mod
import urllib.error
import urllib.request
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.api_error import RpAiApiError

log = logging.getLogger(__name__)

# 判定器的四段输出。顺序就是页面上的顺序，也是提示词里的顺序。
DIAGNOSIS_FIELDS = ("user", "cause", "scope", "action")

PROMPT = """你是 RP AI App Market（内部 AI 资产门户，FastAPI + Angular）的值班工程师。
生产上这个接口返回了 5xx，请给运维负责人一份简短诊断。

出错接口：{method} {path}
状态码：{status}
异常类型：{exc_type}
异常消息：{exc_message}
累计出现次数：{count}

应用层调用栈（已滤掉框架帧；为空表示这次不是崩溃，而是代码显式返回了这个状态码）：
```
{traceback}
```

你手上没有这个项目的源码，也不要去找 —— 只根据上面的信息判断，证据不足就在对应字段里
直接说证据不足。不要描述你的思考过程。

只输出一个 JSON 对象，四个键，值全部用中文，每个值一句话：
{{"user": "给用户的一句话，不含代码术语，说清他现在能不能用、要不要等",
  "cause": "技术根因，指出最可能的那一处",
  "scope": "影响面，明确说是只影响这一个资产、还是这一类接口、还是全站",
  "action": "建议动作，下一步查什么或改什么"}}"""


def fingerprint(method: str, path: str, exc_type: str | None) -> str:
    """同一件事的稳定标识。见 ``models/api_error.py`` 里为什么 path 不做 id 归一化。"""
    raw = f"{method.upper()}|{normalize_path(path)}|{exc_type or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def normalize_path(path: str) -> str:
    """只去掉 query string。

    query 里常带 ``?eid=EZHGMIO`` 这类每次都不同的值，不去掉的话同一个坏接口会按调用者
    裂成很多行。路径本身一个字不动 —— 那是猜测的开始。
    """
    return (path or "").split("?", 1)[0][:512]


def _app_frames(exc: BaseException) -> str:
    """只留本项目的栈帧与最后的异常行。

    完整栈里绝大多数是 starlette / uvicorn / sqlalchemy 的帧，对判断根因没有帮助，还会把
    提示词顶到几万字符（判定器的慢响应与提示词规模强相关）。判据是路径里有没有 ``/app/``
    这一段 —— 容器里本项目的代码都在 ``/app/app/`` 下。

    留不下任何应用帧时不返回空串，而是退回完整栈的尾部：一条只有框架帧的栈仍然比没有栈强。
    """
    lines = tb_mod.format_exception(type(exc), exc, exc.__traceback__)
    text = "".join(lines)
    kept = [
        ln for ln in text.splitlines()
        if "/app/" in ln or not ln.lstrip().startswith("File ")
    ]
    out = "\n".join(kept).strip()
    return (out or text.strip())[-4000:]


def record(
    db: Session,
    *,
    method: str,
    path: str,
    status: int,
    eid: str | None = None,
    exc: BaseException | None = None,
) -> RpAiApiError | None:
    """把一次 5xx 记成（或并入）一行。失败只落日志，绝不抛。

    并入而不是新增，靠 ``fingerprint`` 上的唯一约束保证：并发两次同样的失败里有一个会撞
    IntegrityError，此时回滚重查再累加，而不是让其中一次丢失。
    """
    try:
        exc_type = type(exc).__name__ if exc is not None else None
        exc_message = (str(exc)[:2000] or None) if exc is not None else None
        trace = _app_frames(exc) if exc is not None else None
        fp = fingerprint(method, path, exc_type)
        now = datetime.now()

        row = db.execute(
            select(RpAiApiError).where(RpAiApiError.fingerprint == fp)
        ).scalar_one_or_none()
        if row is None:
            row = RpAiApiError(
                fingerprint=fp,
                method=method.upper()[:10],
                path=normalize_path(path),
                status=status,
                exc_type=exc_type,
                exc_message=exc_message,
                traceback=trace,
                count=1,
                first_seen=now,
                last_seen=now,
                last_eid=eid,
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                # 另一个请求刚插了同一个指纹。回滚重查走累加分支。
                db.rollback()
                row = db.execute(
                    select(RpAiApiError).where(RpAiApiError.fingerprint == fp)
                ).scalar_one_or_none()
                if row is None:
                    raise
                return _touch(db, row, status, exc_message, trace, eid, now)
            return row
        return _touch(db, row, status, exc_message, trace, eid, now)
    except Exception:  # noqa: BLE001 - 记录失败不能影响响应
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.exception("记录接口报错失败: %s %s", method, path)
        return None


def _touch(
    db: Session,
    row: RpAiApiError,
    status: int,
    exc_message: str | None,
    trace: str | None,
    eid: str | None,
    now: datetime,
) -> RpAiApiError:
    """已有指纹再次发生：累加并刷新「最近一次」。

    **刷新 traceback 与 exc_message 会重置诊断吗？不会。** 已有的诊断保留 —— 同一个指纹
    的根因不会因为又发生一次就变了，重跑一遍只是白花一次判定器调用。要重跑有页面上的按钮。
    """
    row.count = (row.count or 0) + 1
    row.last_seen = now
    row.status = status
    if exc_message:
        row.exc_message = exc_message
    if trace:
        row.traceback = trace
    if eid:
        row.last_eid = eid
    # 复现即视为未处理：标记过「已处理」的问题又回来了，是必须重新看见的信号。
    if row.resolved_at is not None:
        row.resolved_at = None
        row.resolved_by = None
    db.commit()
    return row


def _unwrap(text: str) -> dict | None:
    """从判定器的回复里挖出那个 JSON 对象。

    判定器是一个带工具的审计 agent，回复可能是：直接 JSON、包一层
    ``{"response": "<JSON 字符串>"}``、或者 JSON 前后带一段散文（它会自说自话）。三种都要认，
    否则一次本来成功的诊断会被记成失败。
    """
    candidates = [text]
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        candidates.append(match.group(0))
    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        if any(k in parsed for k in DIAGNOSIS_FIELDS):
            return parsed
        for key in ("response", "content", "text", "answer"):
            inner = parsed.get(key)
            if isinstance(inner, str):
                nested = _unwrap(inner)
                if nested:
                    return nested
    return None


def ask_judge(prompt: str) -> str:
    """调 OpenAI 兼容的判定器，回 message content 原文。

    单独一个函数是为了测试能替换掉它 —— 单测不该依赖一个跑在别的容器里的模型。
    """
    cfg = get_settings()
    body = {
        "model": cfg.error_diagnosis_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 900,
    }
    req = urllib.request.Request(
        f"{cfg.error_diagnosis_llm_base.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=cfg.error_diagnosis_timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def diagnose(db: Session, row: RpAiApiError) -> RpAiApiError:
    """给一行出诊断。成功写四段 + ``diagnosed_at``；失败写 ``diagnosis_error``。

    失败时**刻意不写** ``diagnosed_at`` —— 那一列的含义是「已经有结论」，而不是「试过了」。
    留空下一轮自然会重试，这正是判定器偶发超时时想要的行为。
    """
    prompt = PROMPT.format(
        method=row.method,
        path=row.path,
        status=row.status,
        exc_type=row.exc_type or "（无异常对象）",
        exc_message=(row.exc_message or "（无）")[:2000],
        count=row.count,
        traceback=(row.traceback or "（这次不是崩溃，代码显式返回了这个状态码）")[-4000:],
    )
    try:
        raw = ask_judge(prompt)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        row.diagnosis_error = f"{type(exc).__name__}: {exc}"[:300]
        db.commit()
        log.warning("诊断 %s %s 失败: %s", row.method, row.path, exc)
        return row

    fields = _unwrap(raw or "")
    if not fields:
        row.diagnosis_error = f"判定器回复解析不出 JSON: {(raw or '')[:200]}"[:300]
        db.commit()
        return row

    row.diagnosis_user = str(fields.get("user") or "").strip() or None
    row.diagnosis_cause = str(fields.get("cause") or "").strip() or None
    row.diagnosis_scope = str(fields.get("scope") or "").strip() or None
    row.diagnosis_action = str(fields.get("action") or "").strip() or None
    row.diagnosed_at = datetime.now()
    row.diagnosis_error = None
    db.commit()
    return row


def diagnose_pending(db: Session, limit: int = 1, flood_threshold: int = 10) -> dict:
    """给还没有结论的行出诊断，一轮最多 ``limit`` 条。

    上限存在的理由：这跑在 reconciler 的循环里，而一次判定器调用可能几十秒。一次涌进来
    二十个新报错时，宁可分多轮做完，也不要让一轮 sweep 卡住十分钟 —— 那会连带把技能审计
    的收敛也停掉。

    **防洪**：待诊断超过 ``flood_threshold`` 条时这一轮只做 1 条。判定器是与技能审计共用的
    服务（实测它日均只处理 8.5 次模型调用），而涌入通常意味着同一个故障按 id 裂成了很多指纹
    —— 给它的第 50 个变体出诊断没有新信息，但确实会占住判定器。**涌入本身就是信号**，所以
    这里记一条 warning 而不是安静地慢慢磨。
    """
    pending_total = db.execute(
        select(func.count())
        .select_from(RpAiApiError)
        .where(
            RpAiApiError.diagnosed_at.is_(None),
            RpAiApiError.resolved_at.is_(None),
        )
    ).scalar_one()

    flooding = pending_total > flood_threshold
    if flooding:
        log.warning(
            "待诊断 %d 条超过阈值 %d，本轮只诊断 1 条 —— 很可能是同一个故障按 id 裂成了"
            "多个指纹，先去看接口报错页而不是等诊断做完",
            pending_total, flood_threshold,
        )

    rows = db.execute(
        select(RpAiApiError)
        .where(
            RpAiApiError.diagnosed_at.is_(None),
            RpAiApiError.resolved_at.is_(None),
        )
        .order_by(RpAiApiError.last_seen.desc())
        .limit(1 if flooding else limit)
    ).scalars().all()

    done = failed = 0
    for row in rows:
        before = row.diagnosed_at
        diagnose(db, row)
        if row.diagnosed_at != before:
            done += 1
        else:
            failed += 1
    return {
        "examined": len(rows),
        "diagnosed": done,
        "failed": failed,
        "pending": pending_total,
        "flooding": flooding,
    }
