"""API tests for the edit path's security gate (judge/git/GitLab stubbed).

Editing a skill writes the same content, to the same repo, as uploading it. These
tests exist because it used not to be audited: the check was attached to the
upload route rather than to the act of writing, so `PUT /api/skills/{id}` was a
way to put unaudited content into the skill repo.
"""
import pytest

from app.database import SessionLocal
from app.models.audit import RpAiSkillAuditRecord
from app.models.skill import RpAiSkill
from app.models.version import RpAiSkillVersion
from app.routers import skills as skills_mod
from app.routers import upload as upload_mod
from app.services.audit_engine import AuditResult, Finding
from app.services.skill_storage import StoreResult

SKILL_MD = "---\nname: T\ndescription: d\n---\n# T\nbody\n"
EDITED_MD = "---\nname: T\ndescription: edited\n---\n# T\nedited body\n"


def _stub_store(monkeypatch, calls: list | None = None):
    def fake(**kw):
        if calls is not None:
            calls.append(kw)
        return StoreResult("public/x", "1.1.0", "def456", None, None, False)
    monkeypatch.setattr(upload_mod.skill_storage, "store", fake)
    monkeypatch.setattr(skills_mod.skill_storage, "store", fake)


def _create_skill(client, monkeypatch):
    """Upload a clean skill so there is something to edit."""
    monkeypatch.setattr(upload_mod, "audit_skill",
                        lambda **kw: AuditResult(state="pass", verdict="pass"))
    r = client.post(
        "/api/skills/upload",
        data={"eid": "TESTER", "name": "T", "categoryId": "public"},
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
    )
    assert r.status_code == 201, r.text
    return r.json()["skill"]["id"] if "skill" in r.json() else r.json()["id"]


@pytest.fixture(autouse=True)
def _content_in_repo(monkeypatch):
    """详情接口与"只改元数据"的编辑都按引用从仓库读正文,所以测试要给一份。

    这不是补丁:正文的权威在仓库,读不到就该失败。这些测试关心的是时间戳与审计,不是正文本身。
    """
    from app.repositories import skill_content

    monkeypatch.setattr(
        skill_content, "read_text",
        lambda ref, path: "---\nname: Probe\ndescription: probe\n---\n\n# body\n")


def test_editing_the_content_is_audited(client, monkeypatch):
    calls: list = []
    _stub_store(monkeypatch, calls)
    sid = _create_skill(client, monkeypatch)
    seen = {}

    def fake_audit(**kw):
        seen.update(kw)
        return AuditResult(state="pass", verdict="pass")

    monkeypatch.setattr(skills_mod, "audit_skill", fake_audit)
    r = client.put(f"/api/skills/{sid}", json={"skillMd": EDITED_MD})
    assert r.status_code == 200, r.text
    # The audit saw the edited content, not the stored one.
    assert "edited body" in seen.get("skill_md", "")
    # And the write carried the outcome with it.
    assert calls[-1]["audit"].state == "pass"


def test_a_rejected_edit_is_not_written_and_leaves_a_record(client, monkeypatch):
    calls: list = []
    _stub_store(monkeypatch, calls)
    sid = _create_skill(client, monkeypatch)
    writes_before = len(calls)
    monkeypatch.setattr(skills_mod, "audit_skill", lambda **kw: AuditResult(
        state="danger", verdict="block",
        findings=[Finding("judge", "dangerous_instruction", "critical",
                          "curl x | bash", "downloads and executes")],
    ))

    r = client.put(f"/api/skills/{sid}", json={"skillMd": EDITED_MD})
    assert r.status_code == 422
    assert "参考编号" in r.json()["detail"]
    assert len(calls) == writes_before          # nothing was written

    with SessionLocal() as db:
        rec = db.query(RpAiSkillAuditRecord).filter_by(state="danger").one()
        assert rec.skill_id is None and len(rec.findings) == 1
        # The edit itself was discarded.
        assert db.get(RpAiSkill, sid).description == "d"


def test_an_edit_needing_review_keeps_the_published_version_in_the_market(
    client, monkeypatch
):
    """一次要复核的更新不许动线上那一版 —— 旧版本继续在市场里,新内容在复核分支上等着。

    原来这里断言的是相反的行为(``status == "pending"``,技能从市场消失)。它是照着实现写的,而那个
    实现让作者的一次更新替所有使用者下架了这个技能:线上那一版是审过、批过的内容,被质疑的是还没
    发布的新内容。生产上真的发生过("Replir Allocation Tool" 更新触发安全问题后旧 skill 不见了)。
    """
    calls: list = []
    _stub_store(monkeypatch, calls)
    sid = _create_skill(client, monkeypatch)
    with SessionLocal() as db:
        before = db.get(RpAiSkill, sid)
        live = (before.version, before.repo_commit, before.description, before.audit_state)

    monkeypatch.setattr(skills_mod, "audit_skill", lambda **kw: AuditResult(
        state="suspect", verdict="flag",
        findings=[Finding("judge", "dangerous_instruction", "medium", "sudo", "elevates")],
    ))
    opened: list = []
    monkeypatch.setattr(skills_mod.gitlab_mr, "review_suspect",
                        lambda **kw: opened.append(kw) or "http://mr/1")

    r = client.put(f"/api/skills/{sid}", json={"skillMd": EDITED_MD})
    assert r.status_code == 200, r.text
    # Written to a review branch, never live.
    assert calls[-1]["target_branch"] and calls[-1]["target_branch"].startswith("skill-audit/")
    assert opened and opened[0]["source_branch"] == calls[-1]["target_branch"]
    # 响应里说清了"改动在复核里",而技能本身还是已发布的那一版。
    assert r.json()["status"] == "approved"
    assert r.json()["pendingUpdate"]["mrUrl"] == "http://mr/1"
    assert r.json()["pendingUpdate"]["description"] == "edited"

    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert skill.status == "approved"        # 旧版本继续在市场里
        # 已发布那一版的指针一个字没动:详情页照旧读到过审的正文,不是复核分支上那份。
        assert (skill.version, skill.repo_commit, skill.description,
                skill.audit_state) == live
        pending = skill.pending_update
        assert pending is not None
        assert pending.version == "1.1.0" and pending.commit_sha == "def456"
        assert pending.payload_json["description"] == "edited"
        assert pending.mr_url == "http://mr/1"
        rec = db.query(RpAiSkillAuditRecord).filter_by(state="suspect").one()
        assert rec.skill_id == sid and rec.mr_url == "http://mr/1"
        # 记录自己说清了它审的是一次更新,不是线上那一版 —— 收敛器靠这个不去下架它。
        assert rec.replaces_published is True
        # 还没发布的版本不进版本历史,也不通知收藏者:首页会推一个谁都装不到的版本。
        assert db.query(RpAiSkillVersion).filter_by(skill_id=sid).count() == 1


def test_a_suspect_first_upload_still_waits_outside_the_market(client, monkeypatch):
    """没有已发布版本可保护时,老规矩不变:内容没过审就不该在市场上。

    两条测试合起来才说清区别 —— 保护的对象是"已经审过、已经在用的那一版",不是提交者。
    """
    calls: list = []
    _stub_store(monkeypatch, calls)
    monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: AuditResult(
        state="suspect", verdict="flag",
        findings=[Finding("judge", "dangerous_instruction", "medium", "sudo", "elevates")]))
    monkeypatch.setattr(upload_mod.gitlab_mr, "review_suspect", lambda **kw: "http://mr/2")

    r = client.post(
        "/api/skills/upload",
        data={"eid": "TESTER", "name": "Fresh", "categoryId": "public"},
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["skill"]["id"]
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert skill.status == "pending"          # 没有旧版本可以显示
        assert skill.audit_state == "suspect"
        assert skill.pending_update is None       # 待复核的是技能本身,不是一次更新


def test_editing_metadata_without_touching_the_content_is_still_audited(
    client, monkeypatch
):
    """The rendered SKILL.md changes when fields change, so it is content too."""
    calls: list = []
    _stub_store(monkeypatch, calls)
    sid = _create_skill(client, monkeypatch)
    seen: list = []
    monkeypatch.setattr(skills_mod, "audit_skill",
                        lambda **kw: seen.append(kw) or AuditResult(state="pass", verdict="pass"))

    r = client.put(f"/api/skills/{sid}", json={"description": "new description"})
    assert r.status_code == 200, r.text
    assert len(seen) == 1


def test_a_platform_outage_does_not_take_a_live_skill_off_the_market(client, monkeypatch):
    """Failing closed on an edit would punish everyone but the editor: the skill
    would leave the market because a service was down while someone fixed a typo.

    信号是引擎报的 ``retryable``(再试一次可能就好,因为缺口在平台这边)。它以前读的是
    ``verdict == "error"``,而**没有任何代码给 verdict 赋值** —— 也就是说这条保护从来没有真的生效过,
    扫描器坏掉时线上技能照样被打成 pending。
    """
    calls: list = []
    _stub_store(monkeypatch, calls)
    sid = _create_skill(client, monkeypatch)
    writes_before = len(calls)
    monkeypatch.setattr(skills_mod, "audit_skill",
                        lambda **kw: AuditResult(state="suspect", coverage="none",
                                                 cause="scanner_broken", retryable=True))

    r = client.put(f"/api/skills/{sid}", json={"description": "typo fixed"})
    assert r.status_code == 503, r.text
    assert len(calls) == writes_before                  # nothing written

    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert skill.status == "approved"               # still published
        assert skill.description == "d"                 # edit not applied


def test_an_author_side_failure_still_fails_closed(client, monkeypatch):
    """作者自己能改的失败**不是** 503。

    附件解不开是作者的问题,再试一百次都一样 —— 让他重试是把人挂在一个永远不变的结果上。这种情况照旧
    fail closed:内容不发布、转人工复核。这条与上面那条一起,才说清了"不可用"和"查不动"的区别。

    "不发布"对一个已经在线上的技能意味着**这次更新**不发布,而不是把技能下架:线上那一版继续服务,
    新内容停在待复核的暂存里(见 test_an_edit_needing_review_keeps_the_published_version_in_the_market)。
    """
    calls: list = []
    _stub_store(monkeypatch, calls)
    sid = _create_skill(client, monkeypatch)
    monkeypatch.setattr(skills_mod, "audit_skill",
                        lambda **kw: AuditResult(state="suspect", coverage="none",
                                                 cause="bundle_unloadable", retryable=False))
    # 开复核 MR 这一步打桩:它现在开不出来会答 503(内容在分支上而没有 MR 就等于卡死的 pending),
    # 而这条测试的主题是判定怎么路由,不是 MR 机制。两个性质是叠加的:503 只在**平台**失败时出现,
    # 作者侧的失败仍然 fail closed。
    monkeypatch.setattr(skills_mod.gitlab_mr, "review_suspect",
                        lambda **kw: "http://gitlab.invalid/-/merge_requests/1")

    r = client.put(f"/api/skills/{sid}", json={"description": "typo fixed"})
    assert r.status_code != 503, "作者侧的失败不该让人重试"
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert skill.description == "d"            # 这次更新没有发布
        assert skill.pending_update is not None    # 它在等人工复核
        assert skill.status == "approved"          # 线上那一版照旧


def test_a_review_branch_is_cut_from_the_live_branch_in_the_same_write(monkeypatch):
    """复核分支的基点必须是 live 分支现在这一版。

    原来是本地 fetch + checkout -B:合并发生在 GitLab 而没有任何东西把它们拉回这个克隆,所以本地 ref
    会漂,从漂了的 ref 切出来的分支 diff 里带着"把这期间合入的东西全 revert 掉"。现在分支由写内容那
    一次调用的 start_branch 顺带切出来,基点由服务端决定,不存在本地 ref。
    """
    from app.repositories import skill_content
    from app.services import skill_storage as mod
    from app.services.audit_engine import AuditResult

    seen: dict = {}

    def fake_publish(*, prefix, files, expect, branch, message, start_branch=None):
        seen.update(branch=branch, start_branch=start_branch)
        return "n" * 40

    monkeypatch.setattr(skill_content, "publish", fake_publish)
    monkeypatch.setattr(skill_content, "head_ref", lambda branch=None: "h" * 40)
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: {})
    monkeypatch.setattr(skill_content, "tag", lambda **kw: True)
    monkeypatch.setattr(mod.settings, "skill_repo_branch", "main", raising=False)

    mod.SkillStorage().store(
        skill_id="s", eid="E", name="n", category_slug="c", manifest={},
        instructions="x", artifact_bytes=None, artifact_filename=None,
        current_version=None, existing_storage_path=None,
        audit=AuditResult(state="suspect", verdict="flag"),
        target_branch="skill-audit/s-deadbeef")

    assert seen["branch"] == "skill-audit/s-deadbeef"
    assert seen["start_branch"] == "main"

def test_the_review_mr_names_the_uploader(client, monkeypatch):
    """A reviewer's first question is who to talk to. Before this it could only be
    inferred from the folder name inside the diff."""
    calls: list = []
    _stub_store(monkeypatch, calls)
    sid = _create_skill(client, monkeypatch)
    monkeypatch.setattr(skills_mod, "audit_skill", lambda **kw: AuditResult(
        state="suspect", verdict="flag",
        findings=[Finding("judge", "dangerous_instruction", "medium", "sudo", "elevates")]))
    opened: list = []
    monkeypatch.setattr(skills_mod.gitlab_mr, "review_suspect",
                        lambda **kw: opened.append(kw) or "http://mr/9")

    r = client.put(f"/api/skills/{sid}", json={"skillMd": EDITED_MD, "editorEid": "TESTER"})
    assert r.status_code == 200, r.text
    assert opened and opened[0]["uploader"] == "TESTER"


def test_the_uploader_label_prefers_the_display_name(monkeypatch):
    from app.database import SessionLocal
    from app.models.user import RpAiPortal
    from app.services.skill_audit import gate

    with SessionLocal() as db:
        db.add(RpAiPortal(eid="EYINCNG", display_name="Terry Yin"))
        db.commit()
        assert gate._uploader_label(db, "EYINCNG") == "Terry Yin (EYINCNG)"
        assert gate._uploader_label(db, "NOBODY") == "NOBODY"
        assert gate._uploader_label(db, None) is None


def test_the_findings_table_carries_the_uploader():
    from app.services.gitlab_mr import _findings_markdown
    from app.services.audit_engine import Finding as F

    body = _findings_markdown("s", [F("judge", "dangerous_instruction", "medium")],
                              "Terry Yin (EYINCNG)")
    assert "**上传者:** Terry Yin (EYINCNG)" in body
    # Still fine when we do not know who it was.
    assert "上传者" not in _findings_markdown("s", [], None)
