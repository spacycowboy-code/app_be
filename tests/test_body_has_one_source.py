"""正文只有一个来源:仓库。

改之前:`/markdown` 按引用从仓库读,而详情接口 `/api/skills/{id}` 发的是数据库 `instructions` 那一列。
同一份正文两个来源,而响应上看不出用了哪一份;前端的复核界面用的就是数据库那一份去算"文档是否完整"。

改之后那一列不再写、也不再读。这两条测试盯住:详情接口发的是仓库那一份,以及读不到时不拿别的副本顶替。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.repositories import skill_content

OWNER = "EBODY"
SKILL_ID = "body-source-probe"
PATH = "public/body-source-probe_EBODY_Probe"
REF = "b" * 40
IN_DB = "# 数据库里那份旧正文\n"
IN_REPO = "---\nname: Body Probe\ndescription: p\n---\n\n# 仓库里那份当前正文\n"


@pytest.fixture
def skill():
    with SessionLocal() as db:
        if not db.query(RpAiPortal).filter_by(eid=OWNER).first():
            db.add(RpAiPortal(eid=OWNER, display_name="Body Owner"))
            db.flush()
        db.add(RpAiSkill(id=SKILL_ID, name="Body Probe", category_id="public",
                         status="approved", version="1.0.0", tags=[],
                         uploader_eid=OWNER, storage_path=PATH, repo_commit=REF,
                         instructions=IN_DB))
        db.commit()
    yield SKILL_ID
    with SessionLocal() as db:
        row = db.get(RpAiSkill, SKILL_ID)
        if row:
            db.delete(row)
            db.commit()


def test_the_detail_endpoint_serves_the_body_from_the_repo(client, skill, monkeypatch):
    asked: dict = {}

    def fake(ref: str, path: str) -> str:
        asked.update(ref=ref, path=path)
        return IN_REPO

    monkeypatch.setattr(skill_content, "read_text", fake)
    body = client.get(f"/api/skills/{skill}").json()
    assert body["instructions"] == IN_REPO
    assert "数据库里那份旧正文" not in body["instructions"]
    assert asked == {"ref": REF, "path": f"{PATH}/SKILL.md"}


def test_an_unreadable_body_is_503_not_the_database_copy(client, skill, monkeypatch):
    """读不到就失败,和 `/markdown` 一致。

    把这一格填成空更糟:复核界面会把"读不到"算成"这个技能没写文档",而页面本来也显示不出正文
    —— 它走的就是 `/markdown`,那条路同样会失败。
    """
    def boom(ref: str, path: str) -> str:
        raise skill_content.ContentUnavailable("URLError")

    monkeypatch.setattr(skill_content, "read_text", boom)
    resp = client.get(f"/api/skills/{skill}")
    assert resp.status_code == 503
    assert "数据库里那份旧正文" not in resp.text


def test_uploading_does_not_write_the_body_into_the_database(client, monkeypatch):
    """那一列不再有写者。留着写会长出第二份会漂的副本,而且没有重算路径。"""
    from app.services.audit_engine import AuditResult

    monkeypatch.setattr(
        "app.routers.upload.audit_skill",
        lambda **kw: AuditResult(state="pass", verdict="pass", coverage="complete"),
        raising=False)
    monkeypatch.setattr(skill_content, "head_ref", lambda branch=None: "h" * 40)
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: {})
    monkeypatch.setattr(skill_content, "publish", lambda **kw: "n" * 40)
    monkeypatch.setattr(skill_content, "tag", lambda **kw: True)

    md = "---\nname: No DB Body\ndescription: probe\n---\n\n# body\n"
    resp = client.post(
        "/api/skills/upload",
        files={"file": ("SKILL.md", md.encode(), "text/markdown")},
        data={"eid": OWNER, "name": "No DB Body", "categoryId": "public"},
        headers={"X-User-Eid": OWNER},
    )
    assert resp.status_code in (200, 201), resp.text
    sid = resp.json()["skill"]["id"]
    try:
        with SessionLocal() as db:
            assert not db.get(RpAiSkill, sid).instructions, "正文又被写进数据库了"
    finally:
        with SessionLocal() as db:
            row = db.get(RpAiSkill, sid)
            if row:
                db.delete(row)
                db.commit()
