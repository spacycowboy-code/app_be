"""``tools/refile_market_categories.py`` 的归组判定。

这个脚本对生产库只跑一次,所以它的判定必须在这里钉住 —— 特别是两件会静默出错的事:
一是 ``RP CD IVC`` 这种"名字里带 IVC 但其实是成都"的部门,二是 ``author`` 兜底何时可用。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.market import RpAiMarketItem  # noqa: E402
from app.models.user import RpAiPortal  # noqa: E402
from app.services.dept_groups import ensure_group_categories  # noqa: E402
from tools.refile_market_categories import (  # noqa: E402
    _looks_like_signum,
    resolve_for_item,
)


def _person(db, eid: str, dept: str | None, name: str = "X") -> None:
    row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))
    if row is None:
        row = RpAiPortal(eid=eid, display_name=name)
        db.add(row)
    row.department = dept
    row.display_name = name
    db.commit()


@pytest.fixture
def db():
    with SessionLocal() as session:
        ensure_group_categories(session)
        yield session


def test_creator_department_decides(db) -> None:
    _person(db, "ERF0001", "BNEW RTE RP IVC T5 Team 5", "IVC Person")
    item = RpAiMarketItem(id="t-1", name="n", creator="ERF0001", author="Someone")
    group, why = resolve_for_item(db, item)
    assert group == "ivc"
    assert "ERF0001" in why


def test_chengdus_own_ivc_team_goes_to_cd_not_ivc(db) -> None:
    """脚本存在的理由之一。

    ``Time Report agent`` 的登记人在 ``BNEW RTE RP CD IVC T1``,名字和部门都写着 IVC,
    但那是成都的 IVC 团队。按名字猜会把成都的产出记到北京 IVC 名下,而且看不出错。
    """
    _person(db, "ERF0002", "BNEW RTE RP CD IVC T1 Team 1", "CD Person")
    item = RpAiMarketItem(id="t-2", name="Time Report agent", creator="ERF0002")
    group, _ = resolve_for_item(db, item)
    assert group == "cd"
    assert group != "ivc"


def test_author_is_used_only_when_creator_is_missing(db) -> None:
    _person(db, "ERF0003", "BNEW RTE RP PS AI AI & Innovation", "Author Person")
    _person(db, "ERF0004", "BNEW RTE RP QA Quality Assurance", "Creator Person")
    # 有 creator 时 author 不参与
    item = RpAiMarketItem(id="t-3", name="n", creator="ERF0004", author="ERF0003")
    assert resolve_for_item(db, item)[0] == "qa"
    # 没有 creator 才回落到 author
    item2 = RpAiMarketItem(id="t-4", name="n", creator=None, author="ERF0003")
    group, why = resolve_for_item(db, item2)
    assert group == "ai-team"
    assert "author" in why


def test_author_is_matched_case_insensitively(db) -> None:
    """索引与旧数据里工号大小写混用,``eylaixn`` 和 ``EYLAIXN`` 是同一个人。"""
    _person(db, "ERF0005", "BNEW RTE RP PS AI AI & Innovation", "Lower Case")
    item = RpAiMarketItem(id="t-5", name="n", creator=None, author="erf0005")
    assert resolve_for_item(db, item)[0] == "ai-team"


def test_a_display_name_author_is_not_resolved(db) -> None:
    """``Shuo Yang`` 是人名不是工号。按人名查会在同名时把资产记到错的人头上。"""
    _person(db, "ERF0006", "BNEW RTE RP CD SW1 Software 1", "Shuo Yang")
    item = RpAiMarketItem(id="t-6", name="n", creator=None, author="Shuo Yang")
    group, why = resolve_for_item(db, item)
    assert group is None
    assert "不是工号" in why


def test_unresolvable_department_is_left_alone(db) -> None:
    """兜不到就不动。留在原处是看得见、可回退的;写一个错的组两者都不是。"""
    _person(db, "ERF0007", "BNEW RTE EEE GMT Global HW Meth & Tools", "Outside")
    item = RpAiMarketItem(id="t-7", name="n", creator="ERF0007")
    group, why = resolve_for_item(db, item)
    assert group is None
    assert "解不出" in why


def test_creator_without_a_portal_row_is_left_alone(db) -> None:
    item = RpAiMarketItem(id="t-8", name="n", creator="ENOSUCH1")
    group, why = resolve_for_item(db, item)
    assert group is None
    assert "没有行" in why


def test_no_creator_and_no_author_is_left_alone(db) -> None:
    item = RpAiMarketItem(id="t-9", name="n", creator=None, author=None)
    assert resolve_for_item(db, item)[0] is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("EZHGMIO", True),
        ("eylaixn", True),
        ("ZYIJYAN", True),
        ("Shuo Yang", False),
        ("Richard Qiu (EXT)", False),
        ("", False),
        (None, False),
        ("   ", False),
    ],
)
def test_looks_like_signum(value: str | None, expected: bool) -> None:
    assert _looks_like_signum(value) is expected
