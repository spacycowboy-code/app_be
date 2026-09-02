"""``others`` 兜底分组,以及"标签加起来等于总数"这条不变式。

起因是生产上一个数字对不上:Skill Market 的 All 显示 52,各部门标签加起来 49;Agent Market
All 52、标签 45。差额是 ``category_id`` 为空的行 —— 它们计入 All,却不属于任何标签。10 条,
分三种成因:部门映射不到分组(``BNEW RTE RA RD5``,5 条)、人在目录里查得到但没有部门(外部
人员,3 条)、``gitlab-agents`` 同步只写了显示标签没写分组 id(2 条)。

这里最重要的一条是 ``test_the_tabs_add_up_to_the_all_count``:它钉的不是某一个成因,而是"有行
漂在所有标签之外"这件事本身 —— 三个成因只修两个,它仍然是红的。

与 ``test_category_counts.py`` 的关系值得说明:那个文件里
``test_all_counts_exactly_what_the_unfiltered_list_returns`` 的注释明确写着"拿 All 跟列表比,
**不**跟其它标签之和比,因为分类行不存在的 skill 会缺在那个和里却出现在这里"。当时那是对的
—— 无归属的行真实存在,所以那个和本来就不等。现在无归属的行被消掉了,那个和才能拿来断言。
两条测试不矛盾:一条钉 All 与列表一致,一条钉 All 与标签之和一致,坏起来的方式不同。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.services.dept_groups import (
    FALLBACK_GROUP_ID,
    ensure_group_categories,
    group_or_fallback,
    verified_group,
)

#: 线上真实存在的、没有任何门户分组代表它的部门。
RD5 = "BNEW RTE RA RD5 SWD Software Design D"
IVC = "BNEW RTE RP IVC TE Test Environment"
RD5_USER = "ERD5ONE"


@pytest.fixture()
def seeded():
    """分类行齐备(含 ``all`` 与各分组),外加一个 RD5 部门的人。"""
    with SessionLocal() as db:
        if db.get(RpAiCategory, "all") is None:
            db.add(RpAiCategory(id="all", label="All", count=0))
        db.commit()
        ensure_group_categories(db)
        if db.scalar(select(RpAiPortal).where(RpAiPortal.eid == RD5_USER)) is None:
            db.add(RpAiPortal(eid=RD5_USER, display_name="RD5 Person", department=RD5))
            db.commit()
    yield TestClient(app)


def _admin() -> dict[str, str]:
    return {"X-User-Eid": get_settings().audit_admin_eid}


# --- 分组解析 --------------------------------------------------------------


def test_the_others_row_is_created_on_boot(seeded):
    """``ensure_group_categories`` 必须建出 ``others``。

    它不在 ``DEPT_GROUP_MAP`` 里(不是部门前缀),所以那个循环只要没显式带上它就会漏 ——
    而漏掉的后果是 ``group_or_fallback`` 交出一个没有分类行的 slug,也就是一个不存在的标签。
    """
    with SessionLocal() as db:
        row = db.get(RpAiCategory, FALLBACK_GROUP_ID)
        assert row is not None, "others 分类行没有被建出来"
        assert row.label == "Others"

        db.delete(row)
        db.commit()
        assert FALLBACK_GROUP_ID in ensure_group_categories(db), "删掉之后应该被重建"


def test_an_unmapped_department_falls_back_to_others(seeded):
    """RD5 是线上真实案例:部门存在,但没有任何门户分组代表它。"""
    with SessionLocal() as db:
        assert verified_group(db, RD5) is None, "RD5 本来就不该映射到某个 RP 的分组"
        assert group_or_fallback(db, RD5) == FALLBACK_GROUP_ID


def test_a_mapped_department_is_untouched_by_the_fallback(seeded):
    """兜底只接管"推不出来"的情况,不能改变本来推得出的答案。

    这条防的是把兜底写成"总是 others":那样所有资产都挤进一个标签,而只检查"不为空"的
    测试看不出来。
    """
    with SessionLocal() as db:
        assert group_or_fallback(db, IVC) == "ivc"


def test_no_department_at_all_also_falls_back(seeded):
    with SessionLocal() as db:
        assert group_or_fallback(db, None) == FALLBACK_GROUP_ID
        assert group_or_fallback(db, "") == FALLBACK_GROUP_ID


# --- 不变式 ----------------------------------------------------------------


def test_the_tabs_add_up_to_the_all_count(seeded):
    """标签之和必须等于 All —— 这就是用户看到的那个 bug。

    种一条部门推不出分组的 skill(RD5 的人)。修好之前它的 ``category_id`` 是空的:计入 All、
    不在任何标签下,这个断言失败。
    """
    with SessionLocal() as db:
        db.add(
            RpAiSkill(
                id="others-inv-skill",
                name="others invariant skill",
                category_id=group_or_fallback(db, RD5),
                uploader_eid=RD5_USER,
                status="approved",
                version="1.0.0",
                downloads="0",
                rating=0.0,
                review_count=0,
                tags=[],
            )
        )
        db.commit()

    rows = seeded.get("/api/categories", headers=_admin()).json()
    by_id = {r["id"]: r["count"] for r in rows}
    total = by_id.pop("all")
    assert sum(by_id.values()) == total, (
        "各分组标签加起来必须等于 All。差额就是 category_id 落在任何分类行之外的行 —— "
        f"标签之和 {sum(by_id.values())} != All {total}"
    )
    assert by_id[FALLBACK_GROUP_ID] == 1, "那条 RD5 的 skill 应该落在 others 标签下"


def test_nothing_visible_is_left_without_a_group(seeded):
    """换个角度钉同一件事:直接查库,不许有可见的行没有分组。

    上面那条走接口,这条走数据层。两条都留是因为坏法不同:接口那条会被
    ``/api/categories`` 的算法变化掩盖,这条不会。
    """
    with SessionLocal() as db:
        db.add(
            RpAiSkill(
                id="others-db-skill",
                name="others db skill",
                category_id=group_or_fallback(db, RD5),
                uploader_eid=RD5_USER,
                status="approved",
                version="1.0.0",
                downloads="0",
                rating=0.0,
                review_count=0,
                tags=[],
            )
        )
        db.commit()

        for model in (RpAiSkill, RpAiMarketItem):
            homeless = db.scalar(
                select(func.count(model.id)).where(
                    model.status == "approved",
                    (model.category_id.is_(None)) | (model.category_id == ""),
                )
            )
            assert homeless == 0, f"{model.__name__} 有 {homeless} 行 approved 却没有分组"
