"""网关在回 401 之前必须把请求体读完。

这不是鉴权语义,而是"怎么结束一次被拒绝的请求"——所以单独一个文件。

为什么不用 TestClient:它总会把 body 发完,复现不了那个竞态。这里直接驱动 ASGI,
数 receive 被消费到什么程度,断言的是不变量("body 被读完")而不是那个概率性的症状。
理由与代价见 app/main.py 里 ``_discard_body`` 的注释。
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import get_settings
from app.main import app

UPLOAD_PATH = "/api/skills/upload"


@pytest.fixture()
def gate_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "drain-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    return settings


def _scope(method: str, path: str, *, body_len: int | None) -> dict:
    headers = [(b"host", b"testserver")]
    if body_len is not None:
        headers.append((b"content-length", str(body_len).encode()))
        headers.append((b"content-type", b"application/octet-stream"))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 51234),
        "server": ("testserver", 80),
    }


def _drive(scope: dict, chunks: list[bytes]) -> tuple[int, int, int]:
    """把请求喂给应用,返回 (状态码, 未被取走的块数, receive 调用次数)。"""
    pending = list(chunks)
    calls = 0
    status = 0

    async def receive():
        nonlocal calls
        calls += 1
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    asyncio.run(app(scope, receive, send))
    return status, len(pending), calls


def test_gate_drains_body_before_401(gate_on):
    """没有 token 的大 POST:答 401,且请求体已被读完。

    断言"未被取走的块数为 0"而不是断言状态码就够了 —— 状态码在修复前后都是 401,
    真正会引起 502 的是"body 没被读完就关连接"这件事。
    """
    chunks = [b"x" * 65536 for _ in range(8)]
    total = sum(len(c) for c in chunks)
    status, left, _ = _drive(_scope("POST", UPLOAD_PATH, body_len=total), chunks)
    assert status == 401
    assert left == 0, "网关回 401 时请求体还没读完,反代会把它变成 502"


def test_gate_does_not_read_body_when_there_is_none(gate_on):
    """没有 body 的请求不该多走一次 receive。

    GET 占绝大多数;为它们白等一次 receive 是没必要的开销。
    """
    status, _, calls = _drive(_scope("GET", "/api/skills", body_len=None), [])
    assert status == 401
    assert calls == 0, "无 body 的请求不应触发读取"
