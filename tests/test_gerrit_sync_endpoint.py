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
from app.services.auth_tokens import create_token

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


# --- Admin 页面那个入口 -----------------------------------------------------

PATH_NOW = "/api/integrations/gerrit/ai-assets/sync-now"


def test_sync_now_refuses_an_anonymous_caller(client):
    """页面那条入口不在白名单里,而且它自己也查身份 —— 两道,不是一道。

    起初这条测试是想钉住"白名单是精确匹配,所以 ``/ai-assets/sync`` 不会顺带放行
    ``/ai-assets/sync-now``"。用反面探针验证时发现那个担心不成立:把匹配改成 ``startswith``
    之后这条测试照样通过,因为中间件放过去之后 ``require_dashboard_eid`` 看到
    ``request.state.eid`` 是空的,仍然 401。所以这里断言的是真正成立的东西 —— 无凭据调不通 ——
    而不是一个听起来合理但经不起检验的前缀规则。
    """
    assert PATH_NOW not in AUTH_WHITELIST
    assert client.post(PATH_NOW).status_code == 401


def test_sync_now_refuses_a_caller_who_is_not_an_admin(client, monkeypatch):
    """登录了不等于有权限。"""
    monkeypatch.setenv("DASHBOARD_EIDS", "EZHGMIO")
    get_settings.cache_clear()
    monkeypatch.setattr(gerrit_sync, "sync_ai_assets", lambda db: {"ref": "x", "counts": {}})
    token, _ = create_token("ESOMEONE", display_name="Someone")
    r = client.post(PATH_NOW, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert "同步 Gerrit 资产" in r.json()["detail"]


def test_sync_now_lets_an_admin_through(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_EIDS", "EZHGMIO")
    get_settings.cache_clear()
    monkeypatch.setattr(gerrit_sync, "sync_ai_assets", lambda db: {"ref": "abc", "counts": {}})
    token, _ = create_token("EZHGMIO", display_name="Miao")
    r = client.post(PATH_NOW, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == "abc"


def test_sync_now_does_not_depend_on_the_cron_token(client, monkeypatch):
    """token 忘配时页面仍然可用。

    否则会出一个很难查的故障:有权限的人点按钮,得到 503「未启用」,而那个 token 管的根本
    不是他这条路。
    """
    monkeypatch.setenv("GERRIT_SYNC_TOKEN", "")
    monkeypatch.setenv("DASHBOARD_EIDS", "EZHGMIO")
    get_settings.cache_clear()
    monkeypatch.setattr(gerrit_sync, "sync_ai_assets", lambda db: {"ref": "abc", "counts": {}})
    token, _ = create_token("EZHGMIO", display_name="Miao")
    assert client.post(PATH_NOW, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    # 而定时任务那条仍然是关的。
    assert client.post(PATH, headers={"Authorization": "Bearer anything"}).status_code == 503


def test_both_entrances_report_upstream_failures_the_same_way(client, monkeypatch):
    """一个从页面看是 502、从定时任务看是 500 的系统,排查时会先怀疑错的那一层。"""
    def boom(db):
        raise gerrit_content.ContentUnavailable("连不上 Gerrit")

    monkeypatch.setenv("DASHBOARD_EIDS", "EZHGMIO")
    get_settings.cache_clear()
    monkeypatch.setattr(gerrit_sync, "sync_ai_assets", boom)
    token, _ = create_token("EZHGMIO", display_name="Miao")
    assert client.post(PATH_NOW, headers={"Authorization": f"Bearer {token}"}).status_code == 502
    assert client.post(PATH, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 502
