"""读回 IVC 使用记录：``GET /api/integrations/ivc/usage/events``。

口径是操作者定的**最严**那一档：资产是 IVC 的 **且** 操作者部门是 IVC 的。非 IVC 的人对 IVC
资产的操作整条不返回，连脱敏行都不给。

这个文件钉的核心就是那条边界，以及几个不报错的形状：

* **非 IVC 的人的操作一条都不能漏出去。** 这是这个端点唯一真正的风险面 —— 漏出去不会有任何
  报错，而对方拿到的是别的部门同事的行为记录。
* **读凭据与写凭据是两串不同的值。** 复用一串的话，「只想收数据、不想再给数据」这个决定就没有
  对应的动作。这里用「写的凭据打 GET 必须 401」钉住。
* **``limit`` 作用在部门过滤之后。** 反过来的话，一页里可能全被过滤空，看起来像没有数据。
* **成都 IVC 算 ``ivc``。** ``dept_groups`` 里有明确一条 ``BNEW RTE RP CD IVC`` -> ``ivc``
  （2026-09「按功能归属」那次的决定）。所以这里绝不能自己写 ``startswith("BNEW RTE RP IVC")``——
  那会把成都整批漏掉，而且不报错。必须走 ``resolve_group``。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.download import RpAiSkillDownload
from app.models.favorite import RpAiUserFavorite
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal

PATH = "/api/integrations/ivc/usage/events"
READ = "rvc-read-test-secret"
PUSH = "rvc-push-test-secret"

IVC_DEPT = "BNEW RTE RP IVC T4 Team 4"
SWD_DEPT = "BNEW RTE RP SWD Radio SW"
CD_IVC_DEPT = "BNEW RTE RP CD IVC Chengdu IVC"

IVC_PERSON = "EJIALCA"
SWD_PERSON = "ESUNBOX"
CD_PERSON = "EWXXAAK"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", "irrelevant-here")
    monkeypatch.setenv("IVC_READ_TOKEN", READ)
    monkeypatch.setenv("IVC_PUSH_TOKEN", PUSH)
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _seed():
    """一个 IVC 技能、一个非 IVC 技能，三个人分属 IVC / 瑞典 SWD / 成都 IVC。"""
    with SessionLocal() as db:
        db.add_all(
            [
                RpAiSkill(
                    id="ivc-skill-creator", name="skill-creator", status="approved",
                    category_id="public", external_url="https://nexus/x.tar.gz",
                    external_source="nexus-ivc",
                ),
                RpAiSkill(
                    id="anthropics-skill-creator", name="skill-creator",
                    status="approved", category_id="public",
                ),
                RpAiPortal(eid=IVC_PERSON, display_name="Jiale Cao", department=IVC_DEPT),
                RpAiPortal(eid=SWD_PERSON, display_name="Bo Sun A", department=SWD_DEPT),
                RpAiPortal(eid=CD_PERSON, display_name="CD Person", department=CD_IVC_DEPT),
            ]
        )
        db.commit()


def _at(days_ago: int) -> datetime:
    """一个 naive、库本地时区的时刻 —— 与全仓所有时间列一致。"""
    from app.database import db_utc_offset

    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).astimezone(timezone(db_utc_offset())).replace(tzinfo=None)


def _download(skill_id: str, eid: str, department: str | None, days_ago: int = 1):
    row = RpAiSkillDownload(skill_id=skill_id, eid=eid)
    row.department = department
    row.created_at = _at(days_ago)
    return row


def _get(client, **params):
    return client.get(PATH, params=params, headers={"Authorization": f"Bearer {READ}"})


# --- 闸门 -------------------------------------------------------------------


def test_without_a_token_it_refuses(client):
    assert client.get(PATH).status_code == 401


def test_the_push_credential_cannot_read(client):
    """两串值分开的整个意义就在这一条。

    复用同一串的话，「只想收数据、不想再给数据」这个决定就没有对应的动作 —— 吊销它会把推送
    一起停掉。
    """
    r = client.get(PATH, headers={"Authorization": f"Bearer {PUSH}"})
    assert r.status_code == 401


def test_with_no_read_token_configured_it_is_off_not_open(client, monkeypatch):
    """这条路径与 POST 同一个 URL，而白名单是按路径判的 —— 站点级 JWT 不会先挡一层。"""
    monkeypatch.setenv("IVC_READ_TOKEN", "")
    get_settings.cache_clear()
    r = client.get(PATH, headers={"Authorization": f"Bearer {READ}"})
    assert r.status_code == 503
    assert "IVC_READ_TOKEN" in r.json()["detail"]


def test_the_push_endpoint_still_works_when_reading_is_off(client, monkeypatch):
    """两个开关各自独立：只配写不配读是一个合理的部署状态。"""
    monkeypatch.setenv("IVC_READ_TOKEN", "")
    get_settings.cache_clear()
    r = client.post(
        PATH,
        json={"events": [{
            "eventId": "e1", "verb": "download", "kind": "skill",
            "assetId": "skill-creator", "eid": IVC_PERSON,
            "occurredAt": "2026-09-01T14:22:31+08:00",
        }]},
        headers={"Authorization": f"Bearer {PUSH}"},
    )
    assert r.status_code == 202, r.text


# --- 边界：非 IVC 的数据一条都不给 -----------------------------------------


def test_an_ivc_person_on_an_ivc_asset_is_returned(client):
    with SessionLocal() as db:
        db.add(_download("ivc-skill-creator", IVC_PERSON, IVC_DEPT))
        db.commit()

    body = _get(client).json()
    assert body["total"] == 1
    e = body["events"][0]
    assert (e["verb"], e["kind"], e["assetId"]) == ("download", "skill", "skill-creator")
    assert e["eid"] == IVC_PERSON
    # 组 slug，不是 LDAP 全称 —— 全称里带子团队名，那本身就是组织结构信息。
    assert e["department"] == "ivc"
    assert IVC_DEPT not in r_text(body)


def r_text(body) -> str:
    import json

    return json.dumps(body, ensure_ascii=False)


def test_a_non_ivc_person_on_an_ivc_asset_is_not_returned_at_all(client):
    """最严口径：连脱敏行都不给。

    这是这个端点唯一真正的风险面 —— 漏出去不报错，而对方拿到的是别部门同事的行为记录。
    """
    with SessionLocal() as db:
        db.add(_download("ivc-skill-creator", SWD_PERSON, SWD_DEPT))
        db.commit()

    body = _get(client).json()
    assert body["total"] == 0
    assert body["events"] == []
    # 连工号和部门字样都不能出现在响应里。
    assert SWD_PERSON not in r_text(body)
    assert "radio-sw" not in r_text(body)


def test_an_ivc_person_on_a_non_ivc_asset_is_not_returned(client):
    """资产也要是 IVC 的 —— 这个端点回答「我的资产被怎么用」，不是「IVC 的人干了什么」。"""
    with SessionLocal() as db:
        db.add(_download("anthropics-skill-creator", IVC_PERSON, IVC_DEPT))
        db.commit()

    assert _get(client).json()["total"] == 0


def test_a_mixed_batch_returns_only_the_ivc_rows(client):
    with SessionLocal() as db:
        db.add_all([
            _download("ivc-skill-creator", IVC_PERSON, IVC_DEPT, days_ago=3),
            _download("ivc-skill-creator", SWD_PERSON, SWD_DEPT, days_ago=2),
            _download("anthropics-skill-creator", IVC_PERSON, IVC_DEPT, days_ago=1),
        ])
        db.commit()

    body = _get(client).json()
    assert body["total"] == 1
    assert body["events"][0]["eid"] == IVC_PERSON
    assert SWD_PERSON not in r_text(body)


def test_a_row_with_no_department_is_not_returned(client):
    """部门为空不等于 IVC。空着就归不出任何组，宁可不给。"""
    with SessionLocal() as db:
        db.add(_download("ivc-skill-creator", "EUNKNOWN", None))
        db.commit()
    assert _get(client).json()["total"] == 0


def test_chengdu_ivc_counts_as_ivc(client):
    """成都 IVC **算** ``ivc`` —— ``dept_groups`` 里有明确一条 ``BNEW RTE RP CD IVC`` -> ``ivc``。

    这是 2026-09「按功能归属」那次的既定决定：成都 IVC 与北京 IVC 并排在同一个 IVC 标签下。
    所以这里绝不能自己写 ``startswith("BNEW RTE RP IVC")`` —— 那会把成都整批漏掉，而且不报错。
    这条测试就是钉住「必须走 resolve_group」。
    """
    with SessionLocal() as db:
        db.add(_download("ivc-skill-creator", CD_PERSON, CD_IVC_DEPT))
        db.commit()
    body = _get(client).json()
    assert body["total"] == 1
    assert body["events"][0]["eid"] == CD_PERSON


def test_a_chengdu_department_with_no_subteam_is_not_ivc(client):
    """表里刻意没有站点级的 ``BNEW RTE RP CD`` 键（站点不是功能），所以它解析为 None。"""
    with SessionLocal() as db:
        db.add(RpAiPortal(eid="ECDBARE", display_name="CD Bare", department="BNEW RTE RP CD"))
        db.add(_download("ivc-skill-creator", "ECDBARE", "BNEW RTE RP CD"))
        db.commit()
    assert _get(client).json()["total"] == 0


# --- 收藏与 agent -----------------------------------------------------------


def test_a_favourite_is_returned_as_a_like(client):
    with SessionLocal() as db:
        row = RpAiUserFavorite(eid=IVC_PERSON, skill_id="ivc-skill-creator")
        row.created_at = _at(1)
        db.add(row)
        db.commit()

    body = _get(client).json()
    assert body["total"] == 1
    assert body["events"][0]["verb"] == "like"


def test_a_favourite_by_a_non_ivc_person_is_not_returned(client):
    """这张表没有 department 快照列，部门从用户行现取 —— 过滤照样要成立。"""
    with SessionLocal() as db:
        row = RpAiUserFavorite(eid=SWD_PERSON, skill_id="ivc-skill-creator")
        row.created_at = _at(1)
        db.add(row)
        db.commit()

    body = _get(client).json()
    assert body["total"] == 0
    assert SWD_PERSON not in r_text(body)


def test_an_agent_download_is_returned_as_kind_agent(client):
    """今天生产上必然是空的（agent 还没同步进市场），但接进来那天必须看得见。"""
    with SessionLocal() as db:
        db.add(RpAiMarketItem(
            id="ivc-ctr-runner", name="ctr-runner", item_type="agent",
            service_type="code", category_id="public",
        ))
        db.flush()
        row = RpAiMarketDownload(item_id="ivc-ctr-runner", eid=IVC_PERSON)
        row.department = IVC_DEPT
        row.created_at = _at(1)
        db.add(row)
        db.commit()

    body = _get(client).json()
    assert body["total"] == 1
    assert (body["events"][0]["kind"], body["events"][0]["assetId"]) == (
        "agent", "ctr-runner",
    )


# --- 窗口与分页 -------------------------------------------------------------


def test_the_default_window_is_thirty_days(client):
    with SessionLocal() as db:
        db.add_all([
            _download("ivc-skill-creator", IVC_PERSON, IVC_DEPT, days_ago=5),
            _download("ivc-skill-creator", IVC_PERSON, IVC_DEPT, days_ago=45),
        ])
        db.commit()
    assert _get(client).json()["total"] == 1


def test_since_widens_the_window(client):
    with SessionLocal() as db:
        db.add(_download("ivc-skill-creator", IVC_PERSON, IVC_DEPT, days_ago=45))
        db.commit()
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    assert _get(client, since=old).json()["total"] == 1


def test_a_naive_since_is_refused(client):
    """理由与 POST 那侧同一条：裸本地时间在两个站点之间无法还原成时刻。"""
    r = _get(client, since="2026-09-01T00:00:00")
    assert r.status_code == 422
    assert "时区" in r.text


def test_the_limit_applies_after_the_department_filter(client):
    """反过来的话，一页里可能全被非 IVC 的行占满、过滤后为空，看起来像没有数据。"""
    with SessionLocal() as db:
        # 先插 3 条非 IVC 的（时间更早，会排在前面），再插 1 条 IVC 的。
        db.add_all([
            _download("ivc-skill-creator", SWD_PERSON, SWD_DEPT, days_ago=5),
            _download("ivc-skill-creator", SWD_PERSON, SWD_DEPT, days_ago=4),
            _download("ivc-skill-creator", SWD_PERSON, SWD_DEPT, days_ago=3),
            _download("ivc-skill-creator", IVC_PERSON, IVC_DEPT, days_ago=2),
        ])
        db.commit()

    body = _get(client, limit=1).json()
    assert body["total"] == 1
    assert body["events"][0]["eid"] == IVC_PERSON


def test_an_oversized_limit_is_refused(client):
    from app.schemas.external_usage import MAX_FEED_LIMIT

    assert _get(client, limit=MAX_FEED_LIMIT + 1).status_code == 422


def test_events_come_back_in_time_order(client):
    with SessionLocal() as db:
        db.add_all([
            _download("ivc-skill-creator", IVC_PERSON, IVC_DEPT, days_ago=2),
            _download("ivc-skill-creator", IVC_PERSON, IVC_DEPT, days_ago=5),
        ])
        db.commit()
    times = [e["occurredAt"] for e in _get(client).json()["events"]]
    assert times == sorted(times)


def test_an_empty_result_is_a_200_with_zero_not_a_404(client):
    body = _get(client).json()
    assert body["total"] == 0
    assert "generatedAt" in body


# --- 与推送的关系 -----------------------------------------------------------


def test_a_pushed_event_comes_back_through_the_feed(client):
    """推送写进的就是这几张业务表，所以读回时它在里面 —— 这是刻意的，不做区分。

    对方知道自己推过哪些 eventId，要扣掉是他们那边的减法。
    """
    with SessionLocal() as db:
        db.add(RpAiPortal(eid="EZDDGGJ", display_name="Yang Yu B", department=IVC_DEPT))
        db.commit()

    r = client.post(
        PATH,
        json={"events": [{
            "eventId": "rvc-1", "verb": "download", "kind": "skill",
            "assetId": "skill-creator", "eid": "EZDDGGJ",
            "occurredAt": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        }]},
        headers={"Authorization": f"Bearer {PUSH}"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["applied"] == 1

    body = _get(client).json()
    assert body["total"] == 1
    assert body["events"][0]["eid"] == "EZDDGGJ"


def test_the_feed_has_no_event_id_field(client):
    """门户里的人自己点下载不会有上游事件 id，编一个比留空更坏。"""
    with SessionLocal() as db:
        db.add(_download("ivc-skill-creator", IVC_PERSON, IVC_DEPT))
        db.commit()
    assert "eventId" not in _get(client).json()["events"][0]
