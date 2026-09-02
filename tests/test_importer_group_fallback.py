"""两条自动导入路径都必须给资产一个真实分组。

它们是生产上那 10 条无归属行的全部来源(``gerrit-ai-assets`` 8 条、``gitlab-agents`` 2 条),
而且都是**同步管理**的行 —— 只改数据库下次同步就被覆盖回去,所以修必须落在这里。

分三种成因,这里各钉一条:

* 部门映射不到任何门户分组(``BNEW RTE RA RD5``)-> ``others``
* 人在目录里查得到、但**没有部门** -> 记到兜底工号(``ECUIDEN``)名下,分组随之而来
* ``gitlab-agents`` 的 upsert 从来没写 ``category_id``,只写了显示标签
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.routers.market import _upsert_project
from app.services import asset_owner
from app.services.dept_groups import FALLBACK_GROUP_ID, ensure_group_categories
from app.services.gitlab_sync import GitLabProject

FALLBACK_EID = "ECUIDEN"
#: (eid, 显示名, 部门) —— 和 ``_Directory.search`` 的返回形状一致。
TRACY = (FALLBACK_EID, "Tracy Deng", "BNEW RTE RP SWD SW2 Software 2")
#: 线上真实案例:外部人员,目录里查得到,但没有部门。
EXT = ("ZYIJYAN", "Yijian Yan (EXT)", "")
#: 部门存在,但没有门户分组代表它。
RD5 = ("EBAIMXU", "Baimin Xu", "BNEW RTE RA RD5 SWD Software Design D")


@pytest.fixture()
def directory(monkeypatch):
    """按过滤器里出现的工号返回夹具行。"""
    rows = {r[0].lower(): r for r in (TRACY, EXT, RD5)}

    def search(self, flt: str):
        for key, row in rows.items():
            if f"={key})" in flt.lower():
                return [row]
        return []

    monkeypatch.setattr(asset_owner._Directory, "search", search)
    monkeypatch.setattr(asset_owner._Directory, "close", lambda self: None)
    monkeypatch.setenv("GERRIT_OWNER_FALLBACK_EID", FALLBACK_EID)
    get_settings.cache_clear()
    with SessionLocal() as db:
        if db.get(RpAiCategory, "all") is None:
            db.add(RpAiCategory(id="all", label="All"))
        db.commit()
        ensure_group_categories(db)
    yield
    get_settings.cache_clear()


# --- owner 解析 ------------------------------------------------------------


def test_an_owner_without_a_department_is_recorded_under_the_fallback(directory):
    """外部人员在目录里查得到,但没有部门。

    部门是唯一能推出分组的东西,所以"查得到但没部门"对门户的意义和"根本查不到"相同 ——
    没有一个能负责的内部人。两种情况走同一条兜底。
    """
    resolved = asset_owner.OwnerResolver().resolve("ZYIJYAN")

    assert resolved.eid == FALLBACK_EID
    assert resolved.fell_back is True
    assert resolved.department == TRACY[2], "兜底必须带上兜底人的部门,否则分组还是推不出来"
    assert "没有部门" in resolved.how, f"理由要写清是哪一种兜底,实际是 {resolved.how!r}"


def test_an_owner_with_a_department_is_kept(directory):
    """有部门的人不能被兜底吃掉 —— 包括部门映射不到分组的 RD5。

    这条区分了两件不该混起来的事:``RD5`` 的人**仍然是**资产的负责人,只是他的部门没有
    对应分组(那由 ``others`` 解决)。把他也换成 Tracy 会凭空转移归属。
    """
    resolved = asset_owner.OwnerResolver().resolve("EBAIMXU")

    assert resolved.eid == "EBAIMXU"
    assert resolved.fell_back is False
    assert resolved.department == RD5[2]


def test_an_owner_nobody_can_find_still_falls_back(directory):
    """原有行为不能被这次改动破坏。"""
    resolved = asset_owner.OwnerResolver().resolve("Some Team Name")

    assert resolved.eid == FALLBACK_EID
    assert resolved.fell_back is True
    assert "查不到" in resolved.how


# --- gitlab-agents 同步 ----------------------------------------------------


def _project() -> GitLabProject:
    return GitLabProject(
        id=4242,
        name="hw-test-agent",
        description=None,
        web_url="https://gitlab.internal.ericsson.com/rp-ai-agent/hw-test-agent",
        path_with_namespace="RP-AI-AGENT/hw-test-agent",
        topics=[],
        namespace_name="RP-AI-AGENT",
        default_branch="main",
        last_activity_at="2026-09-01T00:00:00Z",
    )


def test_a_gitlab_agent_gets_a_real_group(directory):
    """以前这里只写显示标签,``category_id`` 一直是空的。

    这条路一个 ``creator`` 都不写,所以没有工号可查、推不出部门 —— 归 ``others``。
    """
    with SessionLocal() as db:
        _upsert_project(db, _project(), "agent", "gitlab-agents")
        db.commit()
        item = db.get(RpAiMarketItem, "gl-4242")

    assert item is not None
    assert item.category_id == FALLBACK_GROUP_ID


def test_the_gitlab_label_matches_its_group(directory):
    """标签必须是分类行自己的,不能是 GitLab 命名空间。

    否则卡片上显示 "RP-AI-AGENT" 而按 "Others" 过滤 —— 同一种"两个字段两个口径",
    正是这个 bug 的原始形态。
    """
    with SessionLocal() as db:
        _upsert_project(db, _project(), "agent", "gitlab-agents")
        db.commit()
        item = db.get(RpAiMarketItem, "gl-4242")
        expected = db.get(RpAiCategory, FALLBACK_GROUP_ID).label

    assert item.category == expected
    assert item.category != "RP-AI-AGENT"
