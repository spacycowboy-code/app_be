"""同步端点的闸门。

端点在 ``AUTH_WHITELIST`` 里,所以它自己那道 token 检查是**唯一**的闸门 —— 站点级 JWT 不会
先挡一层。这几条钉的就是那道闸门,以及上游出问题时回什么码:一个把 Gerrit 不可用报成 500 的
端点会让人来查门户,而门户没坏。
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import AUTH_WHITELIST, app
from app.repositories import gerrit_content
from app.routers import gerrit_sync

PATH = "/api/integrations/gerrit/ai-assets/sync"
TOKEN = "gerrit-sync-test-token"


@pytest.fixture
def client(monkeypatch):
    # 站点级 JWT 开着,才能证明白名单那条确实生效。
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", "irrelevant-here")
    monkeypatch.setenv("GERRIT_SYNC_TOKEN", TOKEN)
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def test_the_path_is_whitelisted_so_its_own_token_is_the_only_gate():
    assert PATH in AUTH_WHITELIST


def test_without_a_token_it_refuses(client):
    assert client.post(PATH).status_code == 401


def test_a_wrong_token_is_refused(client):
    r = client.post(PATH, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_a_correct_token_reaches_the_sync(client, monkeypatch):
    monkeypatch.setattr(gerrit_sync, "sync_ai_assets", lambda db: {"ref": "abc", "counts": {}})
    r = client.post(PATH, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == "abc"


def test_with_no_token_configured_the_endpoint_is_off_not_open(client, monkeypatch):
    """token 没配时必须 503。放行才是真正的危险:这个路径不在 JWT 闸门后面。"""
    monkeypatch.setenv("GERRIT_SYNC_TOKEN", "")
    get_settings.cache_clear()
    r = client.post(PATH, headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503
    assert "GERRIT_SYNC_TOKEN" in r.json()["detail"]


def test_missing_credentials_are_503_because_retrying_will_not_help(client, monkeypatch):
    def boom(db):
        raise gerrit_content.NotConfigured("Gerrit 凭据没有配置")

    monkeypatch.setattr(gerrit_sync, "sync_ai_assets", boom)
    r = client.post(PATH, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 503


def test_an_unreachable_gerrit_is_502_not_500(client, monkeypatch):
    """门户没坏,它依赖的那个系统这会儿答不上来 —— 502 说的就是这件事。"""
    def boom(db):
        raise gerrit_content.ContentUnavailable("连不上 Gerrit")

    monkeypatch.setattr(gerrit_sync, "sync_ai_assets", boom)
    r = client.post(PATH, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 502
