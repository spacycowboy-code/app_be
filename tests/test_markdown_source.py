"""`/markdown` 返回的正文来自技能那一行记着的引用,而不是数据库里的副本。

改这个接口之前它没有测试:`_read_skill_md` 里引用一个不存在的名字,265 个测试照样全过。所以这三条
盯的是接口的可观察行为,而不是内部实现。

`rp_ai_skill.instructions` 是同一份正文的第二份副本。回落到它的后果是拿可能过期的内容去编辑或审计,
而响应上看不出来用了哪一份 —— 所以这里要的是**明确失败**。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.skill import RpAiSkill
from app.repositories import skill_content

SKILL_ID = "markdown-source-probe"
REF = "a" * 40
PATH = "team/markdown-source-probe_EXXX_Probe"


@pytest.fixture
def skill_row():
    with SessionLocal() as db:
        db.add(
            RpAiSkill(
                id=SKILL_ID,
                name="Markdown Source Probe",
                category_id="public",
                status="approved",
                version="1.0.0",
                tags=[],
                storage_path=PATH,
                repo_commit=REF,
                instructions="# 数据库里那份旧正文\n",
            )
        )
        db.commit()
    yield
    with SessionLocal() as db:
        row = db.get(RpAiSkill, SKILL_ID)
        if row:
            db.delete(row)
            db.commit()


def test_markdown_comes_from_the_recorded_ref(client, skill_row, monkeypatch):
    asked: dict = {}

    def fake_read_text(ref: str, path: str) -> str:
        asked["ref"], asked["path"] = ref, path
        return "# 仓库里那份当前正文\n"

    monkeypatch.setattr(skill_content, "read_text", fake_read_text)
    resp = client.get(f"/api/skills/{SKILL_ID}/markdown")
    assert resp.status_code == 200
    assert resp.json()["content"] == "# 仓库里那份当前正文\n"
    assert asked == {"ref": REF, "path": f"{PATH}/SKILL.md"}


def test_unreadable_content_is_503_not_the_database_copy(client, skill_row, monkeypatch):
    def boom(ref: str, path: str) -> str:
        raise skill_content.ContentUnavailable("URLError")

    monkeypatch.setattr(skill_content, "read_text", boom)
    resp = client.get(f"/api/skills/{SKILL_ID}/markdown")
    assert resp.status_code == 503
    assert "数据库里那份旧正文" not in resp.text


def test_a_skill_without_a_ref_is_409_not_a_guess(client, monkeypatch):
    with SessionLocal() as db:
        db.add(
            RpAiSkill(
                id="no-ref-probe",
                name="No Ref Probe",
                category_id="public",
                status="approved",
                version="1.0.0",
                tags=[],
                instructions="# 只有数据库里这一份\n",
            )
        )
        db.commit()
    monkeypatch.setattr(skill_content, "read_text",
                        lambda *a: pytest.fail("没有引用时不该去读内容存储"))
    try:
        resp = client.get("/api/skills/no-ref-probe/markdown")
        assert resp.status_code == 409
    finally:
        with SessionLocal() as db:
            row = db.get(RpAiSkill, "no-ref-probe")
            if row:
                db.delete(row)
                db.commit()
