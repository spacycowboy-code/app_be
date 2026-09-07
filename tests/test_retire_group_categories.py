"""``tools/retire_group_categories.py`` —— 把资产搬出一个要退役的分组。

这个脚本对生产库只跑一次,而它做错的两件事都不会报错:

* 少搬一行,然后分组行被删掉。技能侧因为外键 ``SET NULL`` 变成没有分组、市场侧因为没有
  外键而留着一个查不到分类行的 slug —— 两种都在 All 里被计数、在任何标签下都筛不到。
* 只改 ``category_id`` 忘了改 ``category`` 这一列。卡片继续印着旧组名,看起来像"数据没搬
  干净"而不是"少改了一列"。

所以这里钉的主要不是"能搬",而是"搬不动的时候会不会硬搬"以及"删除那一步拦不拦得住"。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.category import RpAiCategory  # noqa: E402
from app.models.market import RpAiMarketItem  # noqa: E402
from app.models.skill import RpAiSkill  # noqa: E402
from app.models.user import RpAiPortal  # noqa: E402
from app.services.dept_groups import ensure_group_categories  # noqa: E402
from tools.retire_group_categories import collect, resolve  # noqa: E402

RETIRING = "test-retiring"

SKILL_IDS = ("rg-skill-sw", "rg-skill-stuck")
ITEM_IDS = ("rg-item-ivc", "rg-item-author", "rg-item-noowner")


def _person(db, eid: str, dept: str | None, name: str = "X") -> None:
    row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))
    if row is None:
        row = RpAiPortal(eid=eid, display_name=name)
        db.add(row)
    row.department = dept
    row.display_name = name


def _skill(skill_id: str, uploader: str | None) -> RpAiSkill:
    return RpAiSkill(
        id=skill_id,
        name=skill_id,
        category_id=RETIRING,
        category="Retiring",
        status="approved",
        uploader_eid=uploader,
        version="1.0.0",
        downloads="0",
        rating=0.0,
        review_count=0,
        tags=[],
    )


def _item(item_id: str, creator: str | None, author: str | None = None) -> RpAiMarketItem:
    return RpAiMarketItem(
        id=item_id,
        name=item_id,
        description="probe",
        item_type="agent",
        service_type="code",
        source="manual",
        category_id=RETIRING,
        category="Retiring",
        tags=[],
        creator=creator,
        author=author,
        status="approved",
    )


@pytest.fixture
def db():
    with SessionLocal() as session:
        ensure_group_categories(session)
        if session.get(RpAiCategory, RETIRING) is None:
            session.add(RpAiCategory(id=RETIRING, label="Retiring", count=0))
        _person(session, "ERG0001", "BNEW RTE RP CD SW1 Software 1", "CD SW")
        _person(session, "ERG0002", "BNEW RTE RP CD IVC T1 Team 1", "CD IVC")
        _person(session, "ERG0003", "BNEW RTE RP CD Radio Products Chengdu", "CD Direct")
        _person(session, "ERG0004", "BNEW RTE RP QA Quality Assurance", "QA Author")
        # 先把分类行落库再插引用它的资产:``rp_ai_skill.category_id`` 有外键,而同一次
        # flush 里的插入顺序不保证分类先于技能。
        session.commit()
        session.add_all(
            [
                _skill("rg-skill-sw", "ERG0001"),
                # 直属成都,目录里没有功能信号 —— 必须搬不动。
                _skill("rg-skill-stuck", "ERG0003"),
                _item("rg-item-ivc", "ERG0002"),
                _item("rg-item-author", None, author="ERG0004"),
                _item("rg-item-noowner", None, author="Some Person"),
            ]
        )
        session.commit()
        yield session
        for skill_id in SKILL_IDS:
            row = session.get(RpAiSkill, skill_id)
            if row:
                session.delete(row)
        for item_id in ITEM_IDS:
            row = session.get(RpAiMarketItem, item_id)
            if row:
                session.delete(row)
        row = session.get(RpAiCategory, RETIRING)
        if row:
            session.delete(row)
        session.commit()


def _by_name(rows) -> dict:
    return {r.name: r for r in rows}


def test_both_tables_are_collected(db) -> None:
    """技能和市场条目都要收进来。

    只收市场那张表是本脚本存在的理由之一:``refile_market_categories.py`` 就只收市场,
    而技能侧的外键 ``SET NULL`` 会在删分组时把漏掉的技能变成没有分组。
    """
    rows = collect(db, {RETIRING})
    tables = {r.table for r in rows}
    assert tables == {"rp_ai_skill", "rp_ai_market_item"}
    assert len(rows) == 5


def test_each_row_goes_to_the_function_its_owner_works_on(db) -> None:
    rows = collect(db, {RETIRING})
    resolve(db, rows, {})
    got = _by_name(rows)
    assert got["rg-skill-sw"].target == "radio-sw"
    assert got["rg-item-ivc"].target == "ivc"


def test_a_row_with_no_functional_signal_is_left_alone(db) -> None:
    """直属成都的人目录里没有子团队代码,解不出功能 —— 不许猜。

    留在原处是看得见、可回退的;写一个错的组两者都不是,而且错的那个组不会有任何人发现,
    因为资产会出现在别人的标签下、上传者自己再也不会去看那一页。
    """
    rows = collect(db, {RETIRING})
    resolve(db, rows, {})
    stuck = _by_name(rows)["rg-skill-stuck"]
    assert stuck.target is None
    assert "解不出" in stuck.reason


def test_an_explicit_assignment_places_a_stuck_row(db) -> None:
    """``--assign`` 是给没有信号的行点名去处的唯一入口。

    做成命令行参数而不是代码里的一张表:这是一次性的人工判断,写进仓库会变成一条没有
    来源、下一个人无法判断对错的规则。
    """
    rows = collect(db, {RETIRING})
    resolve(db, rows, {"ERG0003": "product-system"})
    stuck = _by_name(rows)["rg-skill-stuck"]
    assert stuck.target == "product-system"
    assert "--assign" in stuck.reason


def test_author_is_used_only_when_there_is_no_creator(db) -> None:
    rows = collect(db, {RETIRING})
    resolve(db, rows, {})
    got = _by_name(rows)
    assert got["rg-item-author"].target == "qa"
    assert "author" in got["rg-item-author"].reason
    # creator 在的时候 author 不参与
    assert got["rg-item-ivc"].reason.startswith("creator")


def test_a_display_name_author_is_not_resolved(db) -> None:
    """``Some Person`` 是人名不是工号。同名的两个人会让搬迁变成抛硬币。"""
    rows = collect(db, {RETIRING})
    resolve(db, rows, {})
    row = _by_name(rows)["rg-item-noowner"]
    assert row.target is None
    assert "工号" in row.reason


def test_an_owner_with_an_empty_department_is_reported_as_such(db) -> None:
    """"没有行" 和 "有行但 department 是空的" 是两个不同的问题,修法也不同。

    把它们合成一句话报出来,是此前一次真实的误判:用 ``users.get(eid) is None`` 同时判定
    两种情况,于是 17 件资产的成因被写成"creator 没有行",而实际上每个人都有行。
    """
    _person(db, "ERG0005", None, "No Dept")
    db.add(_item("rg-item-nodept", "ERG0005"))
    db.commit()
    try:
        rows = collect(db, {RETIRING})
        resolve(db, rows, {})
        row = _by_name(rows)["rg-item-nodept"]
        assert row.target is None
        assert "department 是空的" in row.reason
    finally:
        stale = db.get(RpAiMarketItem, "rg-item-nodept")
        if stale:
            db.delete(stale)
        db.commit()
