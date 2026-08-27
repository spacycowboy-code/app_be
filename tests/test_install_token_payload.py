"""安装令牌带出去的那几个字段,就是安装提示词的全部输入。

提示词本体是前端 `buildInstallPrompt()` 里写死的散文,后端一个字的中文都不给 —— 它只给
事实。所以这里钉住的是**契约**:哪些字段在、空的时候是什么形状。少一个字段的表现不是报错,
而是提示词悄悄退回它的兜底分支(「去 README 里原样抄启动命令」),而那正是这套改动要消除的
东西。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.models.market_version import RpAiMarketItemVersion
from app.models.user import RpAiPortal

OWNER = "EZHGMIO"
ITEM_ID = "gl-7777"

FULL_META = {
    "launch": "kiro-cli chat --agent timesheet",
    "runtime": "long-running",
    "keep": [".browser-profile", ".timesheet-runs"],
    "prerequisites": ["先在自己的浏览器里 Exit + Log off，SAP 工号是排他锁"],
    "install": [
        {
            "command": "powershell -ExecutionPolicy Bypass -File .\\setup.ps1",
            "note": "默认策略是 Restricted，会拦下 .ps1。",
        },
    ],
}


def _seed(meta: dict | None) -> None:
    with SessionLocal() as db:
        if db.get(RpAiPortal, OWNER) is None:
            db.add(RpAiPortal(eid=OWNER))
        db.add(RpAiMarketItem(
            id=ITEM_ID, name="Timesheet Agent", item_type="agent", service_type="code",
            source="upload", repo_url="https://gitlab.example/rp-ai/rp-ai-user-agents/ts",
            category_id="public", category="Public", creator=OWNER, version="1.1.0",
            status="approved", tags=[],
        ))
        db.flush()
        db.add(RpAiMarketItemVersion(
            item_id=ITEM_ID, version="1.1.0", commit_sha="abc123", tag="v1.1.0",
            uploader_eid=OWNER, artifact_name="ts.zip", artifact_bytes=1234,
            metadata_json=meta,
            audit_json={"state": "pass", "coverage": "complete", "findings": []},
        ))
        db.commit()


@pytest.fixture
def token_env(monkeypatch):
    """发安装令牌要签 JWT,所以密钥必须显式给。

    不给的话本地会静默借用开发机 `.env` 里的 `AUTH_JWT_SECRET` 而通过,在 CI 上则
    `RuntimeError: auth_jwt_secret is not configured` —— 一个"在我机器上是好的"的测试
    比没有这个测试更糟。
    """
    monkeypatch.setenv("AUTH_JWT_SECRET", "install-token-test-secret")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.routers.market.fetch_head_commit_sha", lambda *a, **k: "deadbeef"
    )
    yield
    get_settings.cache_clear()


def _issue(client) -> dict:
    r = client.post(f"/api/market/{ITEM_ID}/install-token", headers={"X-User-Eid": OWNER})
    assert r.status_code == 200, r.text
    return r.json()


def test_every_field_the_prompt_reads_is_present(client, token_env) -> None:
    _seed(FULL_META)
    issued = _issue(client)

    # 门户自己的机制:下载凭据与版本短路。
    for key in ("token", "expiresIn", "path", "filename", "version"):
        assert issued.get(key), f"缺少 {key}"

    # 逐包声明的部分。提示词对每一项都有一个"没有就退回"的分支,所以它们必须真的到得了。
    assert issued["launch"] == FULL_META["launch"]
    assert issued["runtime"] == "long-running"
    assert issued["keep"] == [".browser-profile", ".timesheet-runs"]
    assert issued["prerequisites"] == FULL_META["prerequisites"]
    assert issued["install"] == FULL_META["install"]


def test_missing_metadata_degrades_to_empty_not_to_absent(client, token_env) -> None:
    """同步来的仓库没有这几项。

    形状要稳定:提示词判的是"空还是非空",而 `KeyError` 与 `undefined` 在前端是两种不同的
    坏法 —— 后者会让整条 `if` 静默走错分支。
    """
    _seed(None)
    issued = _issue(client)

    assert issued["launch"] is None
    assert issued["runtime"] is None
    assert issued["keep"] == []
    assert issued["prerequisites"] == []
    assert issued["install"] == []


def test_partial_metadata_keeps_the_rest_empty(client, token_env) -> None:
    """老版本行里没有 install / prerequisites 这两个键 —— 它们是这次才加的。

    这不是假想的:改动上线前已经存在的版本行就是这个形状,而它们仍然要能发安装令牌。
    """
    _seed({"launch": "python main.py", "runtime": "oneshot", "keep": []})
    issued = _issue(client)

    assert issued["launch"] == "python main.py"
    assert issued["install"] == []
    assert issued["prerequisites"] == []
