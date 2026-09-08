"""``GET /api/market/{item_id}`` —— 详情页与分享链接取单条用的接口。

这里钉住的核心是**可见性闸门**,而不是「能不能取到一条数据」。

下架的实现方式就是可见性:管理员在审核页拒绝,条目 status 变成非 approved,于是它从所有人的
列表里消失。而条目 id 是可推出来的(``gl-<GitLab 项目 id>``)。所以一个按 id 取的新接口若
少了这道闸,等于给任何拿到过链接的人留了一条把已下架条目重新捞出来的路 —— 而且不报错,列表
页看起来一切正常。``list_items`` 的 docstring 早就把这件事写过一次,这个文件是把它变成断言。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.user import RpAiPortal

OWNER = "EOWNERX"
STRANGER = "ESTRANG"
ITEM_ID = "gl-999001"


@pytest.fixture()
def client():
    with SessionLocal() as db:
        db.execute(delete(RpAiMarketItem).where(RpAiMarketItem.id == ITEM_ID))
        for eid in (OWNER, STRANGER):
            if db.get(RpAiPortal, eid) is None:
                db.add(RpAiPortal(eid=eid, display_name=eid))
        db.commit()
    with TestClient(app) as c:
        yield c
    with SessionLocal() as db:
        db.execute(delete(RpAiMarketItem).where(RpAiMarketItem.id == ITEM_ID))
        db.commit()


def _seed(status: str = "approved") -> None:
    with SessionLocal() as db:
        db.execute(delete(RpAiMarketItem).where(RpAiMarketItem.id == ITEM_ID))
        db.add(
            RpAiMarketItem(
                id=ITEM_ID,
                name="Shareable Agent",
                description="detail page probe",
                item_type="agent",
                service_type="code",
                source="upload",
                repo_url="https://gitlab.example/rp-ai/probe",
                category_id="ivc",
                category="IVC",
                creator=OWNER,
                version="1.0.0",
                status=status,
                tags=["probe"],
            )
        )
        db.commit()


def _get(client, eid: str | None):
    headers = {"X-User-Eid": eid} if eid else {}
    return client.get(f"/api/market/{ITEM_ID}", headers=headers)


def test_anyone_can_open_an_approved_item(client) -> None:
    """分享链接的正常情况:上架的条目,谁点开都看得到。"""
    _seed()
    with SessionLocal() as db:
        # 两个不同的人各下一次。计数是按 eid 去重的,所以两行两个 eid = 2。
        db.add(RpAiMarketDownload(item_id=ITEM_ID, eid=OWNER))
        db.add(RpAiMarketDownload(item_id=ITEM_ID, eid=STRANGER))
        db.commit()
    r = _get(client, STRANGER)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == ITEM_ID
    assert body["name"] == "Shareable Agent"
    # 详情页要显示下载次数,和列表页同一个来源(attach_market_stats)。
    #
    # 断言必须是**非零**:这两个字段在 schema 里默认就是 0,所以断言 0 的话,把
    # attach_market_stats 整行删掉测试也照过 —— 反面探针当场证明了这一点。
    assert body["downloadCount"] == 2


def test_a_delisted_item_is_not_reachable_by_id(client) -> None:
    """本文件最要紧的一条。

    下架 = status 变成 rejected。条目 id 可推,所以「列表里没有」不构成闸门。少了这道判断,
    任何人拿一条旧链接就能把下架的条目重新读出来,而且没有任何报错。
    """
    _seed(status="rejected")
    assert _get(client, STRANGER).status_code == 404


def test_a_pending_item_is_not_reachable_by_id(client) -> None:
    _seed(status="pending")
    assert _get(client, STRANGER).status_code == 404


def test_the_uploader_still_sees_their_own_pending_item(client) -> None:
    """否则他不知道自己的东西怎么了 —— 和列表页同一条规则。"""
    _seed(status="pending")
    r = _get(client, OWNER)
    assert r.status_code == 200
    assert r.json()["id"] == ITEM_ID


def test_the_admin_sees_everything(client) -> None:
    _seed(status="rejected")
    r = _get(client, get_settings().audit_admin_eid)
    assert r.status_code == 200


def test_hidden_answers_404_not_403(client) -> None:
    """403 会承认这个 id 存在。

    id 是可推的,所以一个「存在但你不能看」的回答等于把下架条目的清单也交出去了 —— 拿一串
    候选 id 挨个试,403 和 404 的区别就是答案。
    """
    _seed(status="rejected")
    r = _get(client, STRANGER)
    assert r.status_code == 404
    assert "403" not in str(r.status_code)


def test_a_missing_id_answers_404(client) -> None:
    r = client.get("/api/market/gl-does-not-exist", headers={"X-User-Eid": STRANGER})
    assert r.status_code == 404


def test_the_detail_route_does_not_shadow_versions_or_review(client) -> None:
    """``/{item_id}`` 只吃一个路径段,不该把已有的两条子路由遮掉。

    这条不是多余的:新增一条 ``/{参数}`` 路由是会遮挡同级具体路径的经典失误,而症状是另一个
    接口忽然改了返回形状,不是报错。
    """
    _seed()
    assert client.get(
        f"/api/market/{ITEM_ID}/versions", headers={"X-User-Eid": OWNER}
    ).status_code == 200

    # 按状态码判断不了遮挡:``/review`` 对一个已上架条目本来就答 200(它只在非 approved 时
    # 才设闸),而被遮挡的话也是 200。所以看**形状** —— review 的响应带 versions,条目的不带。
    r = client.get(f"/api/market/{ITEM_ID}/review", headers={"X-User-Eid": OWNER})
    assert r.status_code == 200
    assert "versions" in r.json(), "/{item_id}/review 被 /{item_id} 接走了"
