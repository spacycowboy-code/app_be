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
    assert r.status_code == 503
    assert len(calls) == writes_before                  # nothing written

    with SessionLocal() as db:
        skill = db.get(RpAiSkill, sid)
        assert skill.status == "approved"               # still published
        assert skill.description == "d"                 # edit not applied
