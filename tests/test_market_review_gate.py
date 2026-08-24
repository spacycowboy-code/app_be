"""待审核的 agent 包对别人不存在,也取不到。

审计只出结论、不拦截,所以**这道闸门就是唯一的闸门**。它必须落在数据层:条目 id 是可推出来的
(`gl-<GitLab 项目 id>`),"列表里看不见"和"接口取不到"是两件事,少一件就等于没有。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.models.market_version import RpAiMarketItemVersion
from app.models.user import RpAiPortal

OWNER = "EZHGMIO"
STRANGER = "EOTHER1"
ADMIN = "ADMINMM"
ITEM_ID = "gl-4242"


def _seed(status: str = "pending") -> None:
    with SessionLocal() as db:
        for eid in (OWNER, STRANGER, ADMIN):
            if db.get(RpAiPortal, eid) is None:
                db.add(RpAiPortal(eid=eid))
        db.add(RpAiMarketItem(
            id=ITEM_ID, name="Pending Agent", item_type="agent", service_type="code",
            source="upload", repo_url="https://gitlab.example/rp-ai/rp-ai-user-agents/pending",
            category_id="public", category="Public", creator=OWNER, version="1.0.0",
            status=status, tags=[],
        ))
        db.flush()
        db.add(RpAiMarketItemVersion(
            item_id=ITEM_ID, version="1.0.0", commit_sha="abc123", tag="v1.0.0",
            uploader_eid=OWNER, artifact_name="pending.zip", artifact_bytes=1234,
            metadata_json={"launch": "python main.py", "runtime": "oneshot", "keep": []},
            audit_json={"state": "pass", "coverage": "complete", "findings": []},
        ))
        db.commit()


def _ids(client, eid: str | None) -> list[str]:
    headers = {"X-User-Eid": eid} if eid else {}
    return [i["id"] for i in client.get("/api/market", headers=headers).json()]


def test_pending_is_hidden_from_others_and_visible_to_owner(client) -> None:
    _seed()
    assert ITEM_ID not in _ids(client, STRANGER)
    assert ITEM_ID not in _ids(client, None)          # 匿名同样看不到
    assert ITEM_ID in _ids(client, OWNER)             # 上传者要能看到自己的
    assert ITEM_ID in _ids(client, ADMIN)             # 审核人要能看到待审的


def test_approved_is_visible_to_everyone(client) -> None:
    _seed(status="approved")
    assert ITEM_ID in _ids(client, STRANGER)


def test_existing_items_are_visible_without_the_column_being_set(client) -> None:
    """现有条目(以及同步来的)默认就是 approved —— 加这一列不该让市场变空。"""
    with SessionLocal() as db:
        db.add(RpAiMarketItem(
            id="gl-1", name="Synced", item_type="tool", service_type="code",
            source="gitlab-tools", category_id="public", tags=[],
        ))
        db.commit()
    assert "gl-1" in _ids(client, STRANGER)


def test_install_token_refuses_pending_for_others(client) -> None:
    _seed()
    r = client.post(f"/api/market/{ITEM_ID}/install-token", headers={"X-User-Eid": STRANGER})
    assert r.status_code == 403
    assert "审核" in r.json()["detail"]


def test_download_refuses_pending_for_others(client) -> None:
    _seed()
    r = client.post(f"/api/market/{ITEM_ID}/download", headers={"X-User-Eid": STRANGER})
    assert r.status_code == 403


def test_owner_can_install_own_pending_package(client, monkeypatch) -> None:
    """上传者得能装自己那份待审的包 —— 不然他没法验证自己传上去的东西。

    这里要显式给一个签名密钥:发安装令牌要签 JWT。不给的话,本地会静默借用开发机 `.env` 里的
    `AUTH_JWT_SECRET` 而通过,在 CI 上(没有 `.env`)则 `RuntimeError: auth_jwt_secret is not
    configured` —— 一个"在我机器上是好的"的测试比没有这个测试更糟。
    """
    monkeypatch.setenv("AUTH_JWT_SECRET", "review-gate-test-secret")
    get_settings.cache_clear()
    _seed()
    monkeypatch.setattr(
        "app.routers.market.fetch_head_commit_sha", lambda *a, **k: "deadbeef"
    )
    r = client.post(f"/api/market/{ITEM_ID}/install-token", headers={"X-User-Eid": OWNER})
    assert r.status_code == 200
    assert r.json()["path"] == f"/api/market/{ITEM_ID}/download"
    # 设过的 settings 不留给后面的测试:缓存里那份带着这里的密钥。
    get_settings.cache_clear()


def test_versions_are_owner_or_admin_only_while_pending(client) -> None:
    _seed()
    assert client.get(f"/api/market/{ITEM_ID}/versions",
                      headers={"X-User-Eid": STRANGER}).status_code == 403
    body = client.get(f"/api/market/{ITEM_ID}/versions",
                      headers={"X-User-Eid": OWNER}).json()
    assert [v["version"] for v in body] == ["1.0.0"]
    assert body[0]["metadataJson"]["launch"] == "python main.py"


def test_review_page_data_needs_the_facts(client, monkeypatch) -> None:
    """审核人是唯一的闸门,所以这一屏要给出他判断所需的东西。"""
    _seed()
    monkeypatch.setattr(
        "app.services.gitlab_projects.list_tree",
        lambda *a, **k: [{"type": "blob", "path": "main.py", "name": "main.py"}],
    )
    r = client.get(f"/api/market/{ITEM_ID}/review", headers={"X-User-Eid": ADMIN})
    assert r.status_code == 200
    body = r.json()
    assert body["item"]["status"] == "pending"
    assert body["files"] == [{"path": "main.py", "name": "main.py"}]
    assert body["filesError"] is None
    assert body["versions"][0]["auditJson"]["coverage"] == "complete"
    assert body["uploaderName"] == OWNER


def test_review_page_says_why_the_file_list_is_missing(client, monkeypatch) -> None:
    """"包里没有文件"和"问不到 GitLab"不能长得一样 —— 后者不该被当成前者批准。"""
    _seed()

    def boom(*_a, **_k):
        from app.services.gitlab_projects import GitLabWriteError
        raise GitLabWriteError("暂时连不上 GitLab")

    monkeypatch.setattr("app.services.gitlab_projects.list_tree", boom)
    body = client.get(f"/api/market/{ITEM_ID}/review", headers={"X-User-Eid": ADMIN}).json()
    assert body["files"] == []
    assert "GitLab" in body["filesError"]


def test_only_admin_can_approve(client) -> None:
    _seed()
    for eid in (STRANGER, OWNER):
        r = client.post(f"/api/market/{ITEM_ID}/review", json={"status": "approved"},
                        headers={"X-User-Eid": eid})
        assert r.status_code == 403, f"{eid} 不该能批准"


def test_approval_publishes_and_is_recorded(client) -> None:
    _seed()
    before = datetime.now(timezone.utc)
    r = client.post(f"/api/market/{ITEM_ID}/review", json={"status": "approved"},
                    headers={"X-User-Eid": ADMIN})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    # 谁批的、什么时候批的:审计不拦之后,这条记录是唯一能回答"谁放进来的"的东西。
    assert body["reviewedBy"] == ADMIN
    assert body["reviewedAt"] is not None
    assert datetime.fromisoformat(body["reviewedAt"]).replace(tzinfo=timezone.utc) >= before.replace(microsecond=0)
    assert ITEM_ID in _ids(client, STRANGER)


def test_rejection_keeps_it_hidden(client) -> None:
    _seed()
    client.post(f"/api/market/{ITEM_ID}/review", json={"status": "rejected"},
                headers={"X-User-Eid": ADMIN})
    assert ITEM_ID not in _ids(client, STRANGER)
    r = client.post(f"/api/market/{ITEM_ID}/install-token", headers={"X-User-Eid": STRANGER})
    assert r.status_code == 403


def test_decision_vocabulary_is_closed(client) -> None:
    """状态不是自由文本:拼错一个词就等于一个谁都看不见、也没人知道为什么的条目。"""
    _seed()
    r = client.post(f"/api/market/{ITEM_ID}/review", json={"status": "approve"},
                    headers={"X-User-Eid": ADMIN})
    assert r.status_code == 422
