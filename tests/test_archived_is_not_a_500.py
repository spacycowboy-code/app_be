"""一条已下架的技能不许把接口打成 500，也不许留在任何列表里。

2026-09-09 生产上第一次成功下架之后立刻暴露的两个后果，同一个根因：`archived` 是下架写进
`rp_ai_skill.status` 的第四个值，而系统里有几处不知道它存在。

* `SkillStatus` 是 `Literal["pending","approved","rejected"]`，不含 `archived`。于是任何
  把这样一行放进响应模型的读路径都是 pydantic 校验失败 -> 500。实测当时
  `GET /api/skills`（带 X-User-Eid 的管理员、以及上传者本人）和 `GET /api/skills/{id}`
  三个都是 500 —— 技能市场页对这两个人整个是坏的。
* 补上字面量之后，那一行会从"接口报错"变成"下架了却还留在市场上"：可见性过滤只挡别人，
  管理员走不加过滤的分支、上传者本人命中 `uploader_eid == requester`。后者比 500 更难发现，
  所以字面量和过滤必须一起改。

这个文件把两半都钉住，并留一条反面测试保证"按 id 还取得到"没有被顺手一起关掉 —— 那一行留着
是有原因的：下载量、收藏、审计记录都在它身上。
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.schemas.skill import SkillStatus

OWNER = "EOWNER"
ADMIN = "ADMINMM"
STRANGER = "ESTRANGE"
GONE = "gone-probe"
LIVE = "live-probe"


@pytest.fixture(autouse=True)
def _content_in_repo(monkeypatch):
    """正文按引用从仓库读 —— 这些测试关心的是可见性，不是正文。"""
    from app.repositories import skill_content
    monkeypatch.setattr(skill_content, "read_text", lambda ref, path: "# x\n")


def _user(db, eid: str):
    """按 eid 查，不是 `db.get` —— `rp_ai_portal` 的主键是自增 id。"""
    return db.query(RpAiPortal).filter_by(eid=eid).first()


@pytest.fixture
def two_skills():
    """一条已下架、一条在线上 —— 在线那条是正面控制，防止过滤写宽了把所有东西都滤掉。"""
    with SessionLocal() as db:
        for e in (OWNER, ADMIN, STRANGER):
            if _user(db, e) is None:
                db.add(RpAiPortal(eid=e, display_name=e.title()))
        db.flush()
        for sid, status in ((GONE, "archived"), (LIVE, "approved")):
            db.add(
                RpAiSkill(
                    id=sid, name=sid, category_id="public", status=status,
                    version="1.0.0", tags=[], uploader_eid=OWNER,
                    storage_path=f"public/{sid}_EOWNER_P", repo_commit="a" * 40,
                    instructions="# x\n",
                )
            )
        db.commit()
    yield
    with SessionLocal() as db:
        for sid in (GONE, LIVE):
            row = db.get(RpAiSkill, sid)
            if row:
                db.delete(row)
        db.commit()


def _ids(client, eid: str | None):
    headers = {"X-User-Eid": eid} if eid else {}
    resp = client.get("/api/skills", headers=headers)
    assert resp.status_code == 200, f"{eid}: HTTP {resp.status_code} {resp.text[:200]}"
    return [s["id"] for s in resp.json()]


# --- 不许 500 ---------------------------------------------------------------


def test_the_lifecycle_literal_admits_archived():
    """直接钉住那一格。

    少了它的症状不是"过滤掉了"，是整个响应 500 —— 用 schema 去藏一个真实存在的取值，
    藏法就是让接口报错。
    """
    assert "archived" in SkillStatus.__args__


def test_the_market_list_does_not_break_for_the_admin(client, two_skills):
    assert LIVE in _ids(client, ADMIN)


def test_the_market_list_does_not_break_for_the_uploader(client, two_skills):
    assert LIVE in _ids(client, OWNER)


def test_an_archived_skill_can_still_be_fetched_by_id(client, two_skills):
    """那一行留着是为了它身上的门户事实（下载量、收藏、审计记录）。

    所以"从列表里去掉"不等于"取不到"。这条同时保证上面那些过滤没有顺手把详情也关掉。
    """
    resp = client.get(f"/api/skills/{GONE}", headers={"X-User-Eid": ADMIN})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "archived"


# --- 也不许留在列表里 -------------------------------------------------------


@pytest.mark.parametrize("eid", [ADMIN, OWNER, STRANGER, None])
def test_it_is_in_nobody_s_market_list(client, two_skills, eid):
    """管理员和上传者本人也一样。

    这两个身份恰恰是原来看得到的那两个：管理员走不加过滤的分支，上传者本人命中
    `uploader_eid == requester`。
    """
    ids = _ids(client, eid)
    assert GONE not in ids, f"{eid} 的市场页里还有一个已下架的技能"
    assert LIVE in ids, f"{eid} 连在线的那条也看不到了 —— 过滤写宽了"


def test_asking_for_it_explicitly_still_works(client, two_skills):
    """`?status=archived` 是一次刻意的查询，不是市场页的默认视图，所以照旧给得出来。

    这条也是上面那道过滤的边界：它加在"没有显式 status"的分支上，不是无条件的。
    """
    resp = client.get("/api/skills", params={"status": "archived"},
                      headers={"X-User-Eid": ADMIN})
    assert resp.status_code == 200, resp.text
    assert [s["id"] for s in resp.json()] == [GONE]


def test_the_department_page_counts_the_same_for_admin_and_everybody_else(
    client, two_skills
):
    """管理员和其他人在同一个页面上必须看到同一个数字。

    `_only_visible` 的管理员旁路是为了看见待复核的东西，不是为了把撤下的资产算回件数里。
    """
    with SessionLocal() as db:
        _user(db, OWNER).department = "BNEW RTE RP CD PS Performance System"
        db.commit()

    seen = {}
    for eid in (ADMIN, STRANGER):
        resp = client.get("/api/departments/cd/assets", headers={"X-User-Eid": eid})
        assert resp.status_code == 200, f"{eid}: HTTP {resp.status_code} {resp.text[:200]}"
        body = resp.json()
        ids = [s["id"] for s in body["skills"]]
        assert GONE not in ids, f"{eid} 的部门页里还有一个已下架的技能"
        seen[eid] = body["metrics"]["skillCount"]
    assert seen[ADMIN] == seen[STRANGER], f"两个身份看到不同的件数: {seen}"
