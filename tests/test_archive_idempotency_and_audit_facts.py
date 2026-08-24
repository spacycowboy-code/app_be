"""下架、幂等、审计记录:三条这次补上的性质。

它们各自对应一个具体后果:

* 下架只删数据库那一行 → 仓库里的 manifest 还在,收敛器补齐 manifest 那一步一上线就把它扫回来。
* 重传同样的内容 → 版本号白涨一格、多一个提交,因为 manifest 里带着这次要写的版本号,拿它一起比永远
  不相等,`publish` 那道"内容一样就不提交"的闸根本够不到。
* 审计记录排在写内容之后 → 写失败时整条丢掉,跑了几十秒的审计没有痕迹,健康端点的失败率也少算一次。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.audit import RpAiSkillAuditRecord
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.repositories import skill_content
from app.services.audit_engine import AuditResult
from app.services.skill_storage import SkillStorage

OWNER = "EOWNER"
ADMIN = "ADMINMM"
STRANGER = "ESTRANGE"
SKILL_ID = "archive-probe"
PATH = "public/archive-probe_EOWNER_Probe"
REF = "a" * 40


@pytest.fixture
def skill():
    with SessionLocal() as db:
        # uploader_eid 是指向 rp_ai_portal 的外键,套件开了 sqlite 外键检查
        for e in (OWNER, ADMIN, STRANGER):
            if db.get(RpAiPortal, e) is None and not db.query(RpAiPortal).filter_by(eid=e).first():
                db.add(RpAiPortal(eid=e, display_name=e.title()))
        db.flush()
        db.add(RpAiSkill(id=SKILL_ID, name="Archive Probe", category_id="public",
                         status="approved", version="1.0.0", tags=[],
                         uploader_eid=OWNER, storage_path=PATH, repo_commit=REF,
                         instructions="# before\n"))
        db.commit()
    yield SKILL_ID
    with SessionLocal() as db:
        row = db.get(RpAiSkill, SKILL_ID)
        if row:
            db.delete(row)
            db.commit()


@pytest.fixture
def content(monkeypatch):
    """记下对内容存储的写,不真的发出去。"""
    seen: list[dict] = []
    monkeypatch.setattr(skill_content, "publish",
                        lambda **kw: (seen.append(kw), "n" * 40)[1])
    monkeypatch.setattr(skill_content, "head_ref", lambda branch=None: "h" * 40)
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: {})
    monkeypatch.setattr(skill_content, "tag", lambda **kw: True)
    return seen


# --- 下架 -----------------------------------------------------------------


def test_a_stranger_cannot_take_a_skill_off_the_market(client, skill, content):
    resp = client.delete(f"/api/skills/{skill}", headers={"X-User-Eid": STRANGER})
    assert resp.status_code == 403
    assert content == [], "鉴权没过却已经动了仓库"
    with SessionLocal() as db:
        assert db.get(RpAiSkill, skill).status == "approved"


def test_the_uploader_can_take_their_own_skill_off_the_market(client, skill, content):
    resp = client.delete(f"/api/skills/{skill}", headers={"X-User-Eid": OWNER})
    assert resp.status_code == 204
    assert content and content[0]["files"] == {}, "内容没有从仓库移掉"
    assert content[0]["prefix"] == PATH
    assert content[0]["expect"] == REF, "没带前置条件,会静默覆盖别人刚写的"


def test_the_admin_can_too(client, skill, content):
    assert client.delete(f"/api/skills/{skill}",
                         headers={"X-User-Eid": ADMIN}).status_code == 204


def test_taking_it_off_keeps_the_row_and_its_portal_facts(client, skill, content):
    client.delete(f"/api/skills/{skill}", headers={"X-User-Eid": OWNER})
    with SessionLocal() as db:
        row = db.get(RpAiSkill, skill)
        assert row is not None, "那一行被删了 —— 下载量、评价、审计记录跟着没了"
        assert row.status == "archived"


def test_it_disappears_from_the_public_list(client, skill, content):
    client.delete(f"/api/skills/{skill}", headers={"X-User-Eid": OWNER})
    body = client.get("/api/skills", params={"status": "approved"}).json()
    assert SKILL_ID not in [s["id"] for s in body]


# --- 幂等 -----------------------------------------------------------------


def _store(storage, **over):
    kwargs = dict(
        skill_id="idem-probe", eid=OWNER, name="Idem Probe", category_slug="public",
        manifest={"name": "Idem Probe"}, instructions="# same\n",
        artifact_bytes=None, artifact_filename=None, current_version="1.0.0",
        existing_storage_path="public/idem-probe_EOWNER_Idem_Probe",
        audit=AuditResult(state="pass", verdict="pass"),
    )
    kwargs.update(over)
    return storage.store(**kwargs)


def test_re_uploading_identical_content_does_not_bump_the_version(monkeypatch):
    """重传同样的内容既不该涨版本,也不该产生提交。"""
    import json

    prefix = "public/idem-probe_EOWNER_Idem_Probe"
    before = {
        "SKILL.md": b"# same\n",
        "manifest.json": json.dumps({"id": "idem-probe", "version": "1.3.0"}).encode(),
    }
    published: list[dict] = []
    monkeypatch.setattr(skill_content, "head_ref", lambda branch=None: "h" * 40)
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: dict(before))
    monkeypatch.setattr(skill_content, "publish",
                        lambda **kw: (published.append(kw), "n" * 40)[1])
    monkeypatch.setattr(skill_content, "tag", lambda **kw: True)

    res = _store(SkillStorage(), existing_storage_path=prefix)
    assert res.version == "1.3.0", "内容没变却涨了版本"
    assert published == [], "内容没变却发了提交"
    assert res.commit_sha == "h" * 40


def test_a_real_change_still_bumps_the_version(monkeypatch):
    import json

    prefix = "public/idem-probe_EOWNER_Idem_Probe"
    before = {
        "SKILL.md": b"# old\n",
        "manifest.json": json.dumps({"id": "idem-probe", "version": "1.3.0"}).encode(),
    }
    published: list[dict] = []
    monkeypatch.setattr(skill_content, "head_ref", lambda branch=None: "h" * 40)
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: dict(before))
    monkeypatch.setattr(skill_content, "publish",
                        lambda **kw: (published.append(kw), "n" * 40)[1])
    monkeypatch.setattr(skill_content, "tag", lambda **kw: True)

    res = _store(SkillStorage(), existing_storage_path=prefix, instructions="# new\n")
    assert res.version == "1.4.0"
    assert published, "内容变了却没提交"


def test_the_manifest_carries_no_wall_clock_stamp(monkeypatch):
    """manifest 里不该有"我们什么时候存的" —— 那是门户事实,而且它会让幂等永远判不出相等。"""
    import json

    published: list[dict] = []
    monkeypatch.setattr(skill_content, "head_ref", lambda branch=None: "h" * 40)
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: {})
    monkeypatch.setattr(skill_content, "publish",
                        lambda **kw: (published.append(kw), "n" * 40)[1])
    monkeypatch.setattr(skill_content, "tag", lambda **kw: True)
    _store(SkillStorage(), existing_storage_path=None)
    manifest = json.loads(published[0]["files"]["manifest.json"].decode())
    assert "storedAt" not in manifest, manifest


# --- 审计记录 -------------------------------------------------------------


def test_the_audit_record_survives_a_failed_content_write(client, monkeypatch):
    """审计跑过就是跑过。写内容失败不该把这个事实抹掉。"""
    def fake_audit(**kw):
        return AuditResult(state="pass", verdict="pass", coverage="complete")

    monkeypatch.setattr("app.routers.upload.audit_skill", fake_audit, raising=False)
    monkeypatch.setattr(skill_content, "head_ref", lambda branch=None: "h" * 40)
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: {})

    def boom(**kw):
        raise skill_content.ContentUnavailable("URLError")

    monkeypatch.setattr(skill_content, "publish", boom)

    md = "---\nname: Audit Fact\ndescription: probe\n---\n\n# body\n"
    resp = client.post(
        "/api/skills/upload",
        files={"file": ("SKILL.md", md.encode(), "text/markdown")},
        data={"eid": OWNER, "name": "Audit Fact", "categoryId": "public"},
        headers={"X-User-Eid": OWNER},
    )
    assert resp.status_code == 503, resp.text
    with SessionLocal() as db:
        recs = db.query(RpAiSkillAuditRecord).all()
        assert recs, "写内容失败,审计记录整条丢了 —— 跑过的审计没有痕迹"
        assert recs[0].state == "pass"
        assert recs[0].skill_id is None, (
            "新技能那一行没建成,这一格只能是空的(它是外键)")
