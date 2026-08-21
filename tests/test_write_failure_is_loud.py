"""写不成的时候必须明确失败,而且不能留下数据库行。

原来的写入路径在推送失败时**先把数据库写了**,再返回 `pushed=False` 并让前端显示一句"已保存但未同步"
—— 于是市场里看得到那个技能而远端 404,而"已保存"这三个字是错的。

顺序现在固定成先仓库后数据库,所以写失败时那一行还没写。两种失败各有各的答法:

* 同一个技能被并发改了 → 409,让人重新读一遍。**不重试** —— 重试等于拿这次的内容盖掉对方刚写的。
* 读写技能仓失败 → 503,本次操作未保存。
"""

import pytest

from app.database import SessionLocal
from app.models.skill import RpAiSkill
from app.repositories import skill_content
from app.services.audit_engine import AuditResult

EDITED_MD = "---\nname: Edited\ndescription: edited\n---\n\n# Edited\n"


def _pass_audit(monkeypatch):
    def fake_audit(**kw):
        return AuditResult(state="pass", verdict="pass", coverage="complete")

    monkeypatch.setattr("app.routers.skills.audit_skill", fake_audit, raising=False)
    monkeypatch.setattr("app.routers.upload.audit_skill", fake_audit, raising=False)


def _seed(skill_id: str) -> None:
    with SessionLocal() as db:
        db.add(
            RpAiSkill(
                id=skill_id, name="Write Failure Probe", category_id="public",
                status="approved", version="1.0.0", tags=[],
                storage_path=f"public/{skill_id}", repo_commit="a" * 40,
                instructions="# before\n",
            )
        )
        db.commit()


def _drop(skill_id: str) -> None:
    with SessionLocal() as db:
        row = db.get(RpAiSkill, skill_id)
        if row:
            db.delete(row)
            db.commit()


@pytest.fixture
def skill():
    _seed("write-failure-probe")
    yield "write-failure-probe"
    _drop("write-failure-probe")


def _no_content_writes(monkeypatch, boom):
    monkeypatch.setattr(skill_content, "head_ref", lambda branch=None: "h" * 40)
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: {})
    monkeypatch.setattr(skill_content, "tag", lambda **kw: True)
    monkeypatch.setattr(skill_content, "publish", boom)


def test_a_concurrent_change_to_the_same_skill_is_answered_with_409(
    client, skill, monkeypatch
):
    _pass_audit(monkeypatch)

    def boom(**kw):
        raise skill_content.Conflict(
            '{"message":"The file has changed since you started editing it"}')

    _no_content_writes(monkeypatch, boom)
    resp = client.put(f"/api/skills/{skill}", json={"skillMd": EDITED_MD})
    assert resp.status_code == 409, resp.text
    with SessionLocal() as db:
        assert db.get(RpAiSkill, skill).instructions == "# before\n", (
            "写没成功却改了数据库那一行")


def test_an_unreachable_skill_repo_is_answered_with_503(client, skill, monkeypatch):
    _pass_audit(monkeypatch)

    def boom(**kw):
        raise skill_content.ContentUnavailable("URLError")

    _no_content_writes(monkeypatch, boom)
    resp = client.put(f"/api/skills/{skill}", json={"skillMd": EDITED_MD})
    assert resp.status_code == 503, resp.text
    assert "未保存" in resp.text
    with SessionLocal() as db:
        assert db.get(RpAiSkill, skill).instructions == "# before\n"
