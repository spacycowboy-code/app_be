"""``openapi_url``：登记一个工具的 OpenAPI spec 地址，让它能在 Kiro 里被直接调用。

这一列的意义全在"有值 / 没值"这个区别上：rp-ai-mcp 的 dispatcher 只把有值的条目列进
"现在就能调的工具"。所以这里要守的不是某个字符串存对了，而是三件事：

1. 创建时能带上它，读回来还是它（否则作者无法自助登记，功能只存在于开发者本机）；
2. **编辑时不会丢**（创建走 ``model_dump()`` 全量透传，编辑是逐字段赋值 —— 两条路，
   漏一条的表现是"填了、保存了、又不见了"）；
3. 不填时是 ``None``，不是空串 —— dispatcher 用"有没有值"做判断，一个空串会让条目
   声称自己有契约，然后在取契约那一步才失败。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.models.user import RpAiPortal
from app.services.dept_groups import ensure_group_categories

client = TestClient(app)

SPEC_URL = "https://tool.example.internal/api/openapi.json"


@pytest.fixture
def owner() -> str:
    eid = "ETESTOAPI"
    with SessionLocal() as db:
        ensure_group_categories(db)
        row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))
        if row is None:
            row = RpAiPortal(eid=eid, display_name="Test OpenAPI")
            db.add(row)
        row.department = "BNEW RTE RP IVC T5 Team 5"
        db.commit()
    return eid


def _register(eid: str, name: str, **over: object):
    body: dict[str, object] = {
        "name": name,
        "description": "d",
        "itemType": "tool",
        "serviceType": "cloud",
        "serviceUrl": "https://tool.example.internal/",
        "author": "Someone",
    }
    body.update(over)
    return client.post("/api/market", json=body, headers={"X-User-Eid": eid})


def _cleanup(prefix: str) -> None:
    with SessionLocal() as db:
        for item in db.scalars(
            select(RpAiMarketItem).where(RpAiMarketItem.name.like(f"{prefix}%"))
        ).all():
            db.delete(item)
        db.commit()


def test_registering_a_tool_keeps_its_openapi_url(owner: str) -> None:
    _cleanup("oapi-create")
    try:
        r = _register(owner, "oapi-create-1", openapiUrl=SPEC_URL)
        assert r.status_code == 201, r.text
        assert r.json()["openapiUrl"] == SPEC_URL

        item_id = r.json()["id"]
        detail = client.get(f"/api/market/{item_id}", headers={"X-User-Eid": owner})
        assert detail.json()["openapiUrl"] == SPEC_URL
    finally:
        _cleanup("oapi-create")


def test_absent_openapi_url_is_null_not_empty_string(owner: str) -> None:
    """dispatcher 用"有没有值"判断能不能直接调 —— 空串会让条目声称自己有契约。"""
    _cleanup("oapi-absent")
    try:
        r = _register(owner, "oapi-absent-1")
        assert r.status_code == 201, r.text
        assert r.json()["openapiUrl"] is None
    finally:
        _cleanup("oapi-absent")


def test_editing_an_item_does_not_drop_the_openapi_url(owner: str) -> None:
    """创建是全量透传、编辑是逐字段赋值 —— 两条路，这一条最容易漏。"""
    _cleanup("oapi-edit")
    try:
        created = _register(owner, "oapi-edit-1", openapiUrl=SPEC_URL)
        item_id = created.json()["id"]

        body = {
            "name": "oapi-edit-1",
            "description": "改了描述而已",
            "itemType": "tool",
            "serviceType": "cloud",
            "serviceUrl": "https://tool.example.internal/",
            "openapiUrl": SPEC_URL,
            "author": "Someone",
            "categoryId": created.json()["categoryId"],
        }
        r = client.put(
            f"/api/market/{item_id}", json=body, headers={"X-User-Eid": owner}
        )
        assert r.status_code == 200, r.text
        assert r.json()["openapiUrl"] == SPEC_URL

        # 清空也要能生效：一个工具的 API 下线后，作者要能把它撤回成普通条目。
        body["openapiUrl"] = None
        r = client.put(
            f"/api/market/{item_id}", json=body, headers={"X-User-Eid": owner}
        )
        assert r.status_code == 200, r.text
        assert r.json()["openapiUrl"] is None
    finally:
        _cleanup("oapi-edit")


def test_listing_exposes_openapi_url(owner: str) -> None:
    """rp-ai-mcp 是从列表接口一次拿到全部候选的，字段必须出现在列表里而不只在详情里。"""
    _cleanup("oapi-list")
    try:
        _register(owner, "oapi-list-1", openapiUrl=SPEC_URL)
        rows = client.get(
            "/api/market", params={"itemType": "tool"}, headers={"X-User-Eid": owner}
        ).json()
        mine = [r for r in rows if r["name"] == "oapi-list-1"]
        assert mine, "登记的条目没有出现在列表里"
        assert mine[0]["openapiUrl"] == SPEC_URL
    finally:
        _cleanup("oapi-list")
