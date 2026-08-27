"""更新触发人工复核时,已发布的那一版必须继续在市场里。

生产上的那一次:用户更新 "Replir Allocation Tool",新内容触发了安全问题转人工复核,**旧的 skill
从市场上消失了**。机制是两步叠起来 —— 更新直接写在技能行上,``status`` 被打成 ``pending``,而公开
列表按 ``status == "approved"`` 过滤;顺带 ``repo_commit`` 也被指到复核分支那个提交上,于是详情页
按引用读到的是那份没过审的正文。

一次被拦下的更新是"新内容不该上线",不是"这个技能不该上线"。代价应该落在提交更新的人身上(他的
改动要等),而不是落在所有使用者身上(技能没了)。这里的测试守住这条边界的两端:提交时不动线上
那一版,复核结论出来后才动。
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.activity import ACTIVITY_VERSION, RpAiActivity
from app.models.favorite import RpAiUserFavorite
from app.models.pending_update import RpAiSkillPendingUpdate
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.models.version import RpAiSkillVersion
from app.routers import skills as skills_mod
from app.routers import upload as upload_mod
from app.services.audit_engine import AuditResult, Finding
from app.services.skill_audit import reconcile as reconcile_mod
from app.services.skill_storage import StoreResult

SKILL_MD = "---\nname: Replir Allocation Tool\ndescription: allocates\n---\n# T\nbody\n"
EDITED_MD = "---\nname: Replir Allocation Tool\ndescription: now with sudo\n---\n# T\nsudo\n"
SUSPECT = AuditResult(
    state="suspect", verdict="flag",
    findings=[Finding("judge", "dangerous_instruction", "medium", "sudo", "elevates")],
)


@pytest.fixture(autouse=True)
def _content_in_repo(monkeypatch):
    """正文按引用从仓库读,测试里给一份 —— 这些测试关心的是可见性,不是正文。"""
    from app.repositories import skill_content
    monkeypatch.setattr(skill_content, "read_text", lambda ref, path: SKILL_MD)


def _stub_store(monkeypatch, version="1.1.0", commit="newsha"):
    def fake(**kw):
        return StoreResult("public/replir", version, commit, f"replir@{version}", None, True)
    monkeypatch.setattr(upload_mod.skill_storage, "store", fake)
    monkeypatch.setattr(skills_mod.skill_storage, "store", fake)


def _publish_clean(client, monkeypatch) -> str:
    """先上传一个干净的、已发布的技能 —— 这就是后面要保护的那一版。"""
    _stub_store(monkeypatch, version="1.0.0", commit="livesha")
    monkeypatch.setattr(upload_mod, "audit_skill",
                        lambda **kw: AuditResult(state="pass", verdict="pass"))
    r = client.post(
        "/api/skills/upload",
        data={"eid": "TESTER", "name": "Replir Allocation Tool", "categoryId": "public"},
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
    )
    assert r.status_code == 201, r.text
    return r.json()["skill"]["id"]


def _submit_suspect_edit(client, monkeypatch, sid, mr="http://gitlab/-/merge_requests/7"):
    _stub_store(monkeypatch)
    monkeypatch.setattr(skills_mod, "audit_skill", lambda **kw: SUSPECT)
    monkeypatch.setattr(skills_mod.gitlab_mr, "review_suspect", lambda **kw: mr)
    r = client.put(f"/api/skills/{sid}", json={"skillMd": EDITED_MD, "editorEid": "TESTER"})
    assert r.status_code == 200, r.text
    return r.json()


def _market_ids(client, eid: str | None = "STRANGER") -> list[str]:
    headers = {"X-User-Eid": eid} if eid else {}
    return [s["id"] for s in client.get("/api/skills", headers=headers).json()]


def test_the_old_version_is_still_in_the_market_and_still_downloadable(client, monkeypatch):
    sid = _publish_clean(client, monkeypatch)
    _submit_suspect_edit(client, monkeypatch, sid)

    # 别人照样看得见、点得开,而且看到的是过审的那一版。
    assert sid in _market_ids(client)
    assert sid in _market_ids(client, None)
    detail = client.get(f"/api/skills/{sid}").json()
    assert detail["status"] == "approved"
    assert detail["version"] == "1.0.0"
    assert detail["repoCommit"] == "livesha"
    assert detail["description"] == "allocates"
    # 而作者能看到自己那次改动的下场。
    assert detail["pendingUpdate"]["version"] == "1.1.0"
    assert detail["pendingUpdate"]["mrUrl"] == "http://gitlab/-/merge_requests/7"


def test_favoriters_are_not_told_about_a_version_that_is_not_published(client, monkeypatch):
    sid = _publish_clean(client, monkeypatch)
    with SessionLocal() as db:
        db.add(RpAiPortal(eid="FANNN"))
        db.add(RpAiUserFavorite(eid="FANNN", skill_id=sid))
        db.commit()

    _submit_suspect_edit(client, monkeypatch, sid)
    with SessionLocal() as db:
        assert db.query(RpAiActivity).filter_by(
            recipient_eid="FANNN", type=ACTIVITY_VERSION).count() == 0


def test_a_merged_review_publishes_the_staged_update(client, monkeypatch):
    sid = _publish_clean(client, monkeypatch)
    _submit_suspect_edit(client, monkeypatch, sid)
    with SessionLocal() as db:
        db.add(RpAiPortal(eid="FANNN"))
        db.add(RpAiUserFavorite(eid="FANNN", skill_id=sid))
        db.commit()

    monkeypatch.setattr(reconcile_mod.gitlab_mr, "mr_state", lambda url: "merged")
    with SessionLocal() as db:
        reconcile_mod.reconcile_all(db)

    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert skill.status == "approved"
        assert (skill.version, skill.repo_commit) == ("1.1.0", "newsha")
        assert skill.description == "now with sudo"
        assert skill.audit_state == "suspect"       # 可疑,但有人读过之后放行了
        assert skill.pending_update is None         # 暂存搬完就没了
        # 版本历史和通知在**发布**时才发生,所以现在才有第二行、才有那条通知。
        assert {v.version for v in db.query(RpAiSkillVersion).filter_by(skill_id=sid)} == {
            "1.0.0", "1.1.0"}
        assert db.query(RpAiActivity).filter_by(
            recipient_eid="FANNN", type=ACTIVITY_VERSION).count() == 1

    # 再收敛一轮不会推翻它:哪条结论描述现在这一版是记在技能行上的事实,不是按时间戳猜的
    # ——两条记录可能落在同一秒里,那时"最新一条"是不确定的。
    with SessionLocal() as db:
        assert db.get(RpAiSkill, sid).audit_ref is not None
        assert reconcile_mod.reconcile_all(db)["changed"] == {}
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert (skill.status, skill.version, skill.audit_state) == (
            "approved", "1.1.0", "suspect")


def test_a_closed_review_leaves_the_skill_published_and_tells_the_author(client, monkeypatch):
    """复核被否掉,该消失的是那次更新,不是这个技能 —— 而作者要能看到它被否了。"""
    sid = _publish_clean(client, monkeypatch)
    _submit_suspect_edit(client, monkeypatch, sid)

    monkeypatch.setattr(reconcile_mod.gitlab_mr, "mr_state", lambda url: "closed")
    with SessionLocal() as db:
        reconcile_mod.reconcile_all(db)

    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert skill.status == "approved"
        assert (skill.version, skill.description) == ("1.0.0", "allocates")
        # 行留着,状态是"被否了":"在复核里"和"被否了"是两个不同的答案。
        assert skill.pending_update.outcome == "rejected"
    assert sid in _market_ids(client)
    assert client.get(f"/api/skills/{sid}").json()["pendingUpdate"]["outcome"] == "rejected"
    # 再收敛几轮也不会变卦(被否的那一行不再等任何人)。
    with SessionLocal() as db:
        assert reconcile_mod.reconcile_all(db)["changed"] == {}
    with SessionLocal() as db:
        assert db.get(RpAiSkill, sid).status == "approved"


def test_an_open_review_changes_nothing_however_often_it_is_reconciled(client, monkeypatch):
    """收敛器是幂等的,而"还开着"这一格的正确动作是什么都不做。"""
    sid = _publish_clean(client, monkeypatch)
    _submit_suspect_edit(client, monkeypatch, sid)

    monkeypatch.setattr(reconcile_mod.gitlab_mr, "mr_state", lambda url: "opened")
    for _ in range(3):
        with SessionLocal() as db:
            assert reconcile_mod.reconcile_all(db)["changed"] == {}
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert skill.status == "approved" and skill.version == "1.0.0"
        assert skill.pending_update is not None


def test_an_unknown_mr_state_does_not_unpublish(client, monkeypatch):
    """问不到 MR 状态(没令牌、网络不通)时,已发布的那一版继续发布着。

    这是与"技能本身待复核"相反的默认值,而两个都对:那边没有已发布内容,fail closed 的代价落在
    提交者身上;这边有,同一个反射会因为一次问不到而下架一个在用的技能。
    """
    sid = _publish_clean(client, monkeypatch)
    _submit_suspect_edit(client, monkeypatch, sid)

    monkeypatch.setattr(reconcile_mod.gitlab_mr, "mr_state", lambda url: None)
    with SessionLocal() as db:
        reconcile_mod.reconcile_all(db)
    with SessionLocal() as db:
        assert db.get(RpAiSkill, sid).status == "approved"


def test_a_suspect_record_without_a_staged_update_does_not_unpublish(client, monkeypatch):
    """"有可疑记录、没有暂存行"是一个真实会出现的状态,它绝不该导致下架。

    上传路径的审计记录在写内容**之前**就单独提交了(它是数据库自己的事实),所以请求在那之后中途
    失败会留下这样一条记录。收敛器只在能对上暂存行时才处置一次更新。
    """
    sid = _publish_clean(client, monkeypatch)
    _submit_suspect_edit(client, monkeypatch, sid)
    with SessionLocal() as db:
        db.query(RpAiSkillPendingUpdate).filter_by(skill_id=sid).delete()
        db.commit()

    monkeypatch.setattr(reconcile_mod.gitlab_mr, "mr_state", lambda url: "closed")
    with SessionLocal() as db:
        reconcile_mod.reconcile_all(db)
    with SessionLocal() as db:
        assert db.get(RpAiSkill, sid).status == "approved"


def test_re_uploading_a_suspect_version_also_keeps_the_old_one_live(client, monkeypatch):
    """上传一个新版本和编辑走的是同一条规则 —— 闸门挂在"写内容"这件事上,不在某一个路由上。"""
    sid = _publish_clean(client, monkeypatch)
    _stub_store(monkeypatch, version="2.0.0", commit="suspectsha")
    monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: SUSPECT)
    monkeypatch.setattr(upload_mod.gitlab_mr, "review_suspect",
                        lambda **kw: "http://gitlab/-/merge_requests/9")

    r = client.post(
        "/api/skills/upload",
        data={"eid": "TESTER", "name": "Replir Allocation Tool", "categoryId": "public",
              "id": sid, "description": "rewritten"},
        files={"file": ("SKILL.md", EDITED_MD, "text/markdown")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["skill"]["status"] == "approved"
    assert sid in _market_ids(client)

    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert (skill.version, skill.repo_commit) == ("1.0.0", "livesha")
        assert skill.description == "allocates"
        assert skill.pending_update.version == "2.0.0"
        assert skill.pending_update.payload_json["description"] == "rewritten"


def test_a_clean_edit_supersedes_a_staged_update(client, monkeypatch):
    """作者不必等复核才能再改。后一次直接发布的改动就是线上那一版,前一次的暂存不再是"下一版"。

    留着它会在复核合并时把后来的改动盖回去 —— 一个人按了"合并",另一个人的内容就退回去了。
    """
    sid = _publish_clean(client, monkeypatch)
    _submit_suspect_edit(client, monkeypatch, sid)

    _stub_store(monkeypatch, version="1.2.0", commit="cleansha")
    monkeypatch.setattr(skills_mod, "audit_skill",
                        lambda **kw: AuditResult(state="pass", verdict="pass"))
    r = client.put(f"/api/skills/{sid}", json={"description": "clean fix"})
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert (skill.status, skill.version, skill.description) == (
            "approved", "1.2.0", "clean fix")
        assert skill.pending_update is None
