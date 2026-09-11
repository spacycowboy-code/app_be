"""上游托管的市场条目：能下载、计数不虚高、agent 登记得进来。

三件事挤在一个文件里，因为它们是同一个缺陷的三面：市场条目原先没有「包在别处」这个概念，
于是 (a) agent 登记不进来，(b) 登记了也点不动，(c) 点了还会把下载数记高。

钉的都是不报错的形状：

* 外链条目下载答 **307**，不是 409。漏掉 ``externally_hosted`` 那个条件会把刚接进来的 agent
  全挡死，而症状是「按钮点了没反应」。
* **没有包的条目不再记流水。** 原先先记后判，于是每次点击都留下一行「他下载过」+ 一个报错弹窗，
  计数永久偏高且看不出异常。
* **取包失败仍然记流水** —— 这一条是有意的，必须钉住，否则下次有人「顺手统一」两条分支时
  「大家都下不下来」这件事就从计数里消失了。
* agent 落 ``rp_ai_market_item`` 而不是 ``rp_ai_skill``：塞错表会让它出现在 Skill Market 和
  技能的报告口径里，一个分类错误同时污染两边的数字。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.routers import market as market_router
from app.services.auth_tokens import create_token

NEXUS = (
    "https://arm1s10-eiffel239.eiffel.gic.ericsson.se:8443/nexus/content/"
    "repositories/releases/agents/repro-expert/1.2.0/kiro/repro-expert-1.2.0-kiro.tar.gz"
)


@pytest.fixture
def client():
    return TestClient(app)


def _item(item_id: str, **over) -> RpAiMarketItem:
    kw = {
        "id": item_id,
        "name": item_id,
        "item_type": "agent",
        "service_type": "code",
        "category_id": "public",
        "status": "approved",
    }
    kw.update(over)
    return RpAiMarketItem(**kw)


def _downloads(db) -> list[RpAiMarketDownload]:
    return list(db.scalars(select(RpAiMarketDownload)))


# --- 外链下载 ---------------------------------------------------------------


def test_an_external_item_redirects_instead_of_409(client):
    with SessionLocal() as db:
        db.add(_item("ivc-repro-expert", external_url=NEXUS, external_source="nexus-ivc"))
        db.commit()

    r = client.post(
        "/api/market/ivc-repro-expert/download?eid=EZHGMIO", follow_redirects=False
    )
    assert r.status_code == 307, r.text
    assert r.headers["location"] == NEXUS


def test_the_redirect_is_307_so_the_method_is_not_rewritten(client):
    """302 允许客户端把 POST 改成 GET，Nexus 那边看到的就是一个它不认的请求。"""
    with SessionLocal() as db:
        db.add(_item("ivc-repro-expert", external_url=NEXUS, external_source="nexus-ivc"))
        db.commit()
    r = client.post(
        "/api/market/ivc-repro-expert/download?eid=EZHGMIO", follow_redirects=False
    )
    assert r.status_code != 302
    assert r.status_code == 307


def test_an_external_download_is_recorded(client):
    """跳转是一条成功路径 —— 它就是下载，必须记。"""
    with SessionLocal() as db:
        db.add(_item("ivc-repro-expert", external_url=NEXUS, external_source="nexus-ivc"))
        db.commit()

    client.post("/api/market/ivc-repro-expert/download?eid=EZHGMIO", follow_redirects=False)
    with SessionLocal() as db:
        rows = _downloads(db)
        assert len(rows) == 1
        assert (rows[0].item_id, rows[0].eid) == ("ivc-repro-expert", "EZHGMIO")


def test_external_source_alone_does_not_make_it_downloadable(client):
    """判据是有没有地址，不是有没有来源。填了来源忘了地址的条目不该看起来可下载。"""
    with SessionLocal() as db:
        db.add(_item("ivc-broken", external_source="nexus-ivc"))
        db.commit()
    r = client.post("/api/market/ivc-broken/download?eid=EZHGMIO", follow_redirects=False)
    assert r.status_code == 409


# --- 计数不再虚高 -----------------------------------------------------------


def test_an_item_with_no_package_is_409_and_records_nothing(client):
    """原先是先记后判：一次点击留下一行「他下载过」+ 一个报错弹窗，计数永久偏高。"""
    with SessionLocal() as db:
        db.add(_item("manual-thing", source="manual", service_url="https://example/x"))
        db.commit()

    r = client.post("/api/market/manual-thing/download?eid=EZHGMIO", follow_redirects=False)
    assert r.status_code == 409
    with SessionLocal() as db:
        assert _downloads(db) == []


def test_repeated_clicks_on_an_unpackaged_item_still_record_nothing(client):
    """偏差原先是只增不减的 —— 同一个人点五次就是五行。"""
    with SessionLocal() as db:
        db.add(_item("manual-thing", source="manual"))
        db.commit()
    for _ in range(5):
        client.post("/api/market/manual-thing/download?eid=EZHGMIO", follow_redirects=False)
    with SessionLocal() as db:
        assert _downloads(db) == []


def test_a_failed_upstream_fetch_is_still_recorded(client, monkeypatch):
    """**有意的**：「大家都下不下来」必须能从计数里看出来。

    这一条和上面两条是相反方向的断言，缺了它，下次有人"顺手统一"两条分支时这个设计就没了。
    """
    from app.services.gitlab_sync import GitLabError

    def boom(_project_id):
        raise GitLabError("upstream is down")

    monkeypatch.setattr(market_router, "fetch_repo_archive", boom)

    with SessionLocal() as db:
        db.add(_item("gl-142925", source="gitlab-agents"))
        db.commit()

    r = client.post("/api/market/gl-142925/download?eid=EZHGMIO", follow_redirects=False)
    assert r.status_code == 502
    with SessionLocal() as db:
        assert len(_downloads(db)) == 1


# --- agent 登记 -------------------------------------------------------------


def _sync(sync_client, payload):
    """``require_sync_eid`` 只看 ``request.state.eid``，所以必须带真 JWT。

    照 ``test_ivc_index_preview`` 的做法。用 ``X-User-Eid`` 头是不行的 —— 那条兜底只在站点鉴权
    关闭时生效，而这个端点的权限判定读的是 ``request.state.eid``。
    """
    return sync_client.post(
        "/api/integrations/ivc/sync", json=payload, headers=_bearer()
    )


AGENT = {
    "id": "repro-expert",
    "name": "repro-expert",
    "description": "客户站问题复现助手",
    "owner": "EIWEULN",
    "version": "1.2.0",
    "tags": ["dcgm", "radio"],
    "runtimes": [{"runtime": "kiro", "download_url": NEXUS}],
    "created_at": "1788945217",
}


@pytest.fixture(autouse=True)
def _ivc_category():
    from app.models.category import RpAiCategory

    with SessionLocal() as db:
        if db.get(RpAiCategory, "ivc") is None:
            db.add(RpAiCategory(id="ivc", label="IVC"))
            db.commit()


@pytest.fixture(autouse=True)
def _no_ldap(monkeypatch):
    """这个 pod 没有目录服务。让部门查询给一个固定答案，别让每个用例去等超时。"""
    from app.services.asset_owner import DirectoryDepartments

    monkeypatch.setattr(
        DirectoryDepartments, "department_of", lambda self, eid: "BNEW RTE RP IVC T6 Team 6"
    )
    monkeypatch.setattr(DirectoryDepartments, "close", lambda self: None)


@pytest.fixture
def sync_client(monkeypatch):
    """同步端点专用：站点鉴权**打开**。

    不能做成 autouse —— 下载那几条用 ``?eid=`` 传身份，而那条兜底只在鉴权关闭时生效。两组用例
    需要相反的设置，混成一个 autouse fixture 会让其中一半永远走不到被测代码。
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "ivc-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    return TestClient(app)


def _bearer(eid: str = "EZHGMIO") -> dict:
    token, _ = create_token(eid)
    return {"Authorization": f"Bearer {token}"}


def test_an_agent_is_registered_in_the_market_not_the_skill_table(sync_client):
    r = _sync(sync_client, {"agents": [AGENT], "skills": []})
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, "ivc-repro-expert")
        assert item is not None
        assert item.item_type == "agent"
        # 可下载的包，不是在线服务。写 cloud 会让卡片去展示一个不存在的服务地址。
        assert item.service_type == "code"
        assert item.external_url == NEXUS
        assert item.external_source == "nexus-ivc"
        assert item.source == "nexus-ivc"
        assert item.category_id == "ivc"
        # 塞进技能表会让它同时出现在 Skill Market 和技能的报告口径里。
        assert db.get(RpAiSkill, "ivc-repro-expert") is None


def test_the_registered_agent_is_immediately_downloadable(sync_client):
    """登记完就能下 —— 这三件事本来就是同一个缺陷的三面。

    这条鉴权开着，所以下载也走 JWT 而不是 ``?eid=``。顺带证明外链下载在真实鉴权下也是通的。
    """
    assert _sync(sync_client, {"agents": [AGENT], "skills": []}).status_code == 200
    r = sync_client.post(
        "/api/market/ivc-repro-expert/download",
        headers=_bearer(),
        follow_redirects=False,
    )
    assert r.status_code == 307
    assert r.headers["location"] == NEXUS


def test_the_agent_owner_gets_a_user_row_with_a_department(sync_client):
    """报告的归属链被 rp_ai_portal 框死，这一行没有的话 agent 落进「无法归属」且不报错。"""
    assert _sync(sync_client, {"agents": [AGENT], "skills": []}).status_code == 200
    with SessionLocal() as db:
        row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == "EIWEULN"))
        assert row is not None
        assert row.department == "BNEW RTE RP IVC T6 Team 6"
        assert row.origin != "login"


def test_syncing_twice_updates_rather_than_duplicates(sync_client):
    assert _sync(sync_client, {"agents": [AGENT], "skills": []}).status_code == 200
    bumped = {**AGENT, "version": "1.3.0"}
    r = _sync(sync_client, {"agents": [bumped], "skills": []})
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        items = list(db.scalars(select(RpAiMarketItem)))
        assert len(items) == 1
        assert items[0].version == "1.3.0"


def test_the_chosen_runtime_is_reported_back(sync_client):
    """一个 agent 可以有多个 runtime，门户只登记一个。不说出来操作者会以为都进来了。"""
    r = _sync(sync_client, {"agents": [AGENT], "skills": []})
    body = r.json()
    detail = next(o["detail"] for o in body["outcomes"] if o["id"] == "ivc-repro-expert")
    assert "kiro" in detail


def test_the_repo_and_service_urls_stay_empty(sync_client):
    """填了会让下载路径去 GitLab 找一个不存在的项目。"""
    assert _sync(sync_client, {"agents": [AGENT], "skills": []}).status_code == 200
    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, "ivc-repro-expert")
        assert item.repo_url is None
        assert item.service_url is None
