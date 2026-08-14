"""API tests for the edit path's security gate (judge/git/GitLab stubbed).

Editing a skill writes the same content, to the same repo, as uploading it. These
tests exist because it used not to be audited: the check was attached to the
upload route rather than to the act of writing, so `PUT /api/skills/{id}` was a
way to put unaudited content into the skill repo.
"""
from app.database import SessionLocal
from app.models.audit import RpAiSkillAuditRecord
from app.models.skill import RpAiSkill
from app.routers import skills as skills_mod
from app.routers import upload as upload_mod
from app.services.skill_audit.types import AuditResult, Finding
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


def test_an_edit_needing_review_goes_to_a_branch_and_leaves_the_market(
    client, monkeypatch
):
    calls: list = []
    _stub_store(monkeypatch, calls)
    sid = _create_skill(client, monkeypatch)
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

    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert skill.status == "pending"        # hidden until the MR is merged
        assert skill.audit_state == "suspect"
        rec = db.query(RpAiSkillAuditRecord).filter_by(state="suspect").one()
        assert rec.skill_id == sid and rec.mr_url == "http://mr/1"


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


def test_a_judge_outage_does_not_take_a_live_skill_off_the_market(client, monkeypatch):
    """Failing closed on an edit would punish everyone but the editor: the skill
    would leave the market because a service was down while someone fixed a typo."""
    calls: list = []
    _stub_store(monkeypatch, calls)
    sid = _create_skill(client, monkeypatch)
    writes_before = len(calls)
    monkeypatch.setattr(skills_mod, "audit_skill",
                        lambda **kw: AuditResult(state="suspect", verdict="error"))

    r = client.put(f"/api/skills/{sid}", json={"description": "typo fixed"})
    assert r.status_code == 503, r.text
    assert len(calls) == writes_before                  # nothing written

    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert skill.status == "approved"               # still published
        assert skill.description == "d"                 # edit not applied


def test_a_review_branch_is_cut_from_the_remote_live_ref(monkeypatch, tmp_path):
    """Merges happen in GitLab and nothing pulls them back into this clone, so a
    branch cut from the local ref carries "revert everything merged since"."""
    from app.services import skill_storage as mod
    from app.services.skill_audit.types import AuditResult

    st = mod.SkillStorage()
    monkeypatch.setattr(st, "repo", tmp_path, raising=False)
    monkeypatch.setattr(mod.settings, "skill_repo_push", True, raising=False)
    calls: list[tuple] = []

    class Done:
        returncode = 0
        stderr = ""

    def fake_git(*args, check=True):
        calls.append(args)
        return Done()

    monkeypatch.setattr(st, "_git", fake_git)
    monkeypatch.setattr(st, "_current_sha", lambda: "abc1234", raising=False)
    monkeypatch.setattr(st, "_push_with_guard", lambda *a, **k: True, raising=False)
    st.store(skill_id="s", eid="E", name="n", category_slug="c", manifest={},
             instructions="x", artifact_bytes=None, artifact_filename=None,
             current_version=None, existing_storage_path=None,
             audit=AuditResult(state="suspect", verdict="flag"),
             target_branch="skill-audit/s-deadbeef")
    fetched = [c for c in calls if c[:1] == ("fetch",)]
    checkout = [c for c in calls if c[:2] == ("checkout", "-B")]
    assert fetched, "must synchronise before cutting the review branch"
    assert checkout and checkout[0][3] == "FETCH_HEAD", checkout


# --- a review says who uploaded the skill -------------------------------------

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
    from app.services.skill_audit.types import Finding as F

    body = _findings_markdown("s", [F("judge", "dangerous_instruction", "medium")],
                              "Terry Yin (EYINCNG)")
    assert "**上传者:** Terry Yin (EYINCNG)" in body
    # Still fine when we do not know who it was.
    assert "上传者" not in _findings_markdown("s", [], None)
