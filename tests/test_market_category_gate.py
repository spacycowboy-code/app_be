"""成都分组、分类行补齐,以及手工登记时的分组闸门。

这三件事是一个改动的三半:映射表加了 ``cd``、库里得有 ``cd`` 那一行、登记时不能再静默
落进一个默认值。少任何一半,症状都是"看起来正常但归错了组",而这正是原来那个缺陷的形状。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.models.user import RpAiPortal
from app.services.dept_groups import (
    DEPT_GROUP_MAP,
    GROUP_CATEGORY_LABELS,
    ensure_group_categories,
    resolve_group,
)

client = TestClient(app)


# --------------------------------------------------------------------------
# 映射表
# --------------------------------------------------------------------------

def test_chengdu_resolves_to_cd() -> None:
    assert resolve_group("BNEW RTE RP CD Radio Products Chengdu") == "cd"
    assert resolve_group("BNEW RTE RP CD SW1 Software 1") == "cd"


def test_chengdus_own_ivc_and_ps_teams_do_not_land_in_beijings_groups() -> None:
    """这条是整个改动里最要紧的一条断言。

    ``BNEW RTE RP CD IVC T1`` 里有 IVC 三个字母,但它是成都自己的 IVC 团队。归到 ``ivc``
    会把成都的产出记在北京 IVC 名下,而界面上一切正常 —— 没有任何地方会报错。靠的是最长
    前缀匹配:``BNEW RTE RP CD`` 有四段,先命中,永远轮不到 ``BNEW RTE RP IVC``。
    """
    assert resolve_group("BNEW RTE RP CD IVC T1 Team 1") == "cd"
    assert resolve_group("BNEW RTE RP CD PS Something") == "cd"
    # 而北京的 IVC / PS 不受影响
    assert resolve_group("BNEW RTE RP IVC T5 Team 5") == "ivc"
    assert resolve_group("BNEW RTE RP PS FS Function System") == "product-system"


def test_ps_ai_still_beats_ps() -> None:
    """加 cd 不该动到原来那条更长前缀优先的规则。"""
    assert resolve_group("BNEW RTE RP PS AI AI & Innovation") == "ai-team"


def test_outside_rp_still_has_no_group() -> None:
    for dept in (
        "BNEW RTE EEE GMT Something",
        "BNEW RCE CCI CEE Something",
        "BNEW RTE RA RD5 Something",
        "BCSS SD SDU MA Something",
    ):
        assert resolve_group(dept) is None, dept


def test_every_mapped_group_has_a_label() -> None:
    """没有标签的 slug 建不出分类行,而 ``ensure_group_categories`` 只会 warning。

    所以这条测试是那个 warning 的替身:让"加了映射忘了标签"在 CI 里就挂,而不是等到
    生产上某个部门的人默认永远填不上。
    """
    missing = sorted(set(DEPT_GROUP_MAP.values()) - set(GROUP_CATEGORY_LABELS))
    assert missing == [], f"这些分组没有标签: {missing}"


# --------------------------------------------------------------------------
# 分类行补齐
# --------------------------------------------------------------------------

def test_ensure_group_categories_creates_missing_rows() -> None:
    with SessionLocal() as db:
        existing = db.get(RpAiCategory, "cd")
        if existing is not None:
            db.delete(existing)
            db.commit()

        created = ensure_group_categories(db)
        assert "cd" in created
        row = db.get(RpAiCategory, "cd")
        assert row is not None
        assert row.label == "Chengdu"


def test_ensure_group_categories_is_idempotent_and_keeps_a_renamed_label() -> None:
    """每次容器启动都会跑一遍,所以它绝不能覆盖人改过的标签。

    ``POST /api/categories`` 允许改名,一个开机就跑的迁移把它改回去,症状是"我明明改了
    名字怎么又变回来了" —— 而这个症状离原因非常远。
    """
    with SessionLocal() as db:
        ensure_group_categories(db)
        row = db.get(RpAiCategory, "cd")
        assert row is not None
        row.label = "成都"
        db.commit()

        created = ensure_group_categories(db)
        assert "cd" not in created
        assert db.get(RpAiCategory, "cd").label == "成都"

        # 还原,免得影响别的测试
        db.get(RpAiCategory, "cd").label = "Chengdu"
        db.commit()


# --------------------------------------------------------------------------
# 登记时的分组闸门
# --------------------------------------------------------------------------

@pytest.fixture
def ivc_person() -> str:
    """一个部门解得出分组的人。"""
    eid = "ETESTIVC"
    with SessionLocal() as db:
        ensure_group_categories(db)
        row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))
        if row is None:
            row = RpAiPortal(eid=eid, display_name="Test IVC")
            db.add(row)
        row.department = "BNEW RTE RP IVC T5 Team 5"
        db.commit()
    return eid


@pytest.fixture
def chengdu_person() -> str:
    eid = "ETESTCD"
    with SessionLocal() as db:
        ensure_group_categories(db)
        row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))
        if row is None:
            row = RpAiPortal(eid=eid, display_name="Test CD")
            db.add(row)
        row.department = "BNEW RTE RP CD IVC T1 Team 1"
        db.commit()
    return eid


@pytest.fixture
def groupless_person() -> str:
    eid = "ETESTNOG"
    with SessionLocal() as db:
        row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))
        if row is None:
            row = RpAiPortal(eid=eid, display_name="Test NoGroup")
            db.add(row)
        row.department = "BNEW RTE EEE GMT Something"
        db.commit()
    return eid


def _register(eid: str, name: str, **over: object) -> object:
    body: dict[str, object] = {
        "name": name,
        "description": "d",
        "itemType": "agent",
        "serviceType": "cloud",
        "serviceUrl": "https://example.com/a",
        "author": "Someone",
    }
    body.update(over)
    return client.post("/api/market", json=body, headers={"X-User-Eid": eid})


def _cleanup(name_prefix: str) -> None:
    with SessionLocal() as db:
        for item in db.scalars(
            select(RpAiMarketItem).where(RpAiMarketItem.name.like(f"{name_prefix}%"))
        ).all():
            db.delete(item)
        db.commit()


def test_empty_category_falls_back_to_the_registrants_group(ivc_person: str) -> None:
    """没传分组时用登记人的部门 —— 这是"默认归属到他对应的组"的服务端那一半。"""
    _cleanup("gate-fallback")
    try:
        r = _register(ivc_person, "gate-fallback-1", categoryId="")
        assert r.status_code == 201, r.text
        assert r.json()["categoryId"] == "ivc"
    finally:
        _cleanup("gate-fallback")


def test_chengdu_registrant_gets_cd_not_ivc(chengdu_person: str) -> None:
    """部门里带 IVC 字样的成都人,兜底必须给 cd。"""
    _cleanup("gate-cd")
    try:
        r = _register(chengdu_person, "gate-cd-1", categoryId="")
        assert r.status_code == 201, r.text
        assert r.json()["categoryId"] == "cd"
    finally:
        _cleanup("gate-cd")


def test_no_category_and_no_group_is_refused(groupless_person: str) -> None:
    """兜不到就必须拒绝,而不是挑一个看起来正常的默认值。"""
    _cleanup("gate-refuse")
    try:
        r = _register(groupless_person, "gate-refuse-1", categoryId="")
        assert r.status_code == 400
        assert "分组" in r.json()["detail"]
    finally:
        _cleanup("gate-refuse")


@pytest.mark.parametrize("pseudo", ["all", "official"])
def test_pseudo_categories_are_refused(ivc_person: str, pseudo: str) -> None:
    """``all`` 是筛选器不是归属地,``official`` 声称"我们审过"。

    注意这条即使兜底能成功也必须拒绝 —— 显式传了一个非法值时,静默改成别的比报错更糟。
    """
    _cleanup("gate-pseudo")
    try:
        r = _register(ivc_person, "gate-pseudo-1", categoryId=pseudo)
        assert r.status_code == 400
        assert pseudo in r.json()["detail"]
    finally:
        _cleanup("gate-pseudo")


def test_unknown_category_is_refused(ivc_person: str) -> None:
    """写进一个没有对应行的 slug,条目在任何 tab 下都筛不到,只有 All 能看见。"""
    _cleanup("gate-unknown")
    try:
        r = _register(ivc_person, "gate-unknown-1", categoryId="no-such-group")
        assert r.status_code == 400
        assert "no-such-group" in r.json()["detail"]
    finally:
        _cleanup("gate-unknown")


def test_explicit_choice_still_wins_over_the_fallback(chengdu_person: str) -> None:
    """默认只是默认。成都人主动选 radio-sw 就该落 radio-sw,否则"他可以改"是假的。"""
    _cleanup("gate-explicit")
    try:
        r = _register(chengdu_person, "gate-explicit-1", categoryId="radio-sw")
        assert r.status_code == 201, r.text
        assert r.json()["categoryId"] == "radio-sw"
    finally:
        _cleanup("gate-explicit")
