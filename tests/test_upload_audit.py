"""API tests for the upload security gate (judge/git/GitLab stubbed).

Mirrors the manual checks: danger -> 422 not stored; pass -> stored+visible;
suspect -> stored, hidden from public browse, Draft MR opened.
"""
from app.routers import upload as upload_mod
from app.database import SessionLocal
from app.models.skill import RpAiSkill
from app.models.audit import RpAiSkillAuditRecord
from app.services.skill_audit.types import AuditResult, Finding
from app.services.skill_storage import StoreResult

SKILL_MD = "---\nname: T\ndescription: d\n---\n# T\nbody\n"


def _stub_store(monkeypatch):
    monkeypatch.setattr(
        upload_mod.skill_storage, "store",
        lambda **kw: StoreResult("public/x", "1.0.0", "abc123", None, None, False),
    )


def _stub_audit(monkeypatch, result: AuditResult):
    monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: result)


def _upload(client, name):
    return client.post(
        "/api/skills/upload",
        data={"eid": "TESTER", "name": name, "categoryId": "public"},
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
    )


def test_danger_rejected_and_not_stored(client, monkeypatch):
    _stub_audit(monkeypatch, AuditResult(
        state="danger", verdict="block",
        findings=[Finding("judge", "dangerous_instruction", "critical", "rm -rf", "destructive")],
    ))
    _stub_store(monkeypatch)
    r = _upload(client, "Dangerous")
    assert r.status_code == 422
    assert "参考编号" in r.json()["detail"]
    db = SessionLocal()
    assert db.query(RpAiSkill).count() == 0            # never stored
    rec = db.query(RpAiSkillAuditRecord).one()
    assert rec.state == "danger" and rec.skill_id is None and len(rec.findings) == 1


def test_pass_stored_and_publicly_visible(client, monkeypatch):
    _stub_audit(monkeypatch, AuditResult(state="pass", verdict="pass"))
    _stub_store(monkeypatch)
    assert _upload(client, "Good").status_code == 201
    ids = [x["id"] for x in client.get("/api/skills").json()]
    assert "good" in ids


def test_suspect_hidden_from_public_visible_to_admin_and_opens_mr(client, monkeypatch):
    _stub_audit(monkeypatch, AuditResult(
        state="suspect", verdict="flag",
        findings=[Finding("judge", "malicious_artifact", "medium", "app.zip", "bundles code")],
    ))
    _stub_store(monkeypatch)
    calls = {}
    monkeypatch.setattr(upload_mod.gitlab_mr, "review_suspect",
                        lambda **kw: calls.update(kw) or "http://mr/1")
    assert _upload(client, "Sus").status_code == 201

    public = [x["id"] for x in client.get("/api/skills").json()]
    admin = [x["id"] for x in client.get("/api/skills", headers={"X-User-Eid": "ADMINMM"}).json()]
    assert "sus" not in public
    assert "sus" in admin
    assert calls.get("skill_name") == "Sus"           # MR was opened
    rec = SessionLocal().query(RpAiSkillAuditRecord).filter_by(state="suspect").one()
    assert rec.mr_url == "http://mr/1"


def test_uploader_sees_own_suspect(client, monkeypatch):
    _stub_audit(monkeypatch, AuditResult(state="suspect", verdict="flag",
                                         findings=[Finding("judge", "prompt_injection", "medium")]))
    _stub_store(monkeypatch)
    monkeypatch.setattr(upload_mod.gitlab_mr, "review_suspect", lambda **kw: None)
    _upload(client, "Mine")
    mine = [x["id"] for x in client.get("/api/skills", headers={"X-User-Eid": "TESTER"}).json()]
    assert "mine" in mine


# --- evaluate (audit-only preview: no store) --------------------------------

def _evaluate(client, name):
    return client.post(
        "/api/skills/evaluate",
        data={"eid": "TESTER", "name": name},
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
    )


def test_evaluate_pass_returns_state_and_stores_nothing(client, monkeypatch):
    _stub_audit(monkeypatch, AuditResult(state="pass", verdict="pass"))
    r = _evaluate(client, "Good")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "pass" and body["reference"]
    assert body["findings"] == []
    db = SessionLocal()
    assert db.query(RpAiSkill).count() == 0             # nothing published
    assert db.query(RpAiSkillAuditRecord).count() == 0  # pass leaves no record


def test_evaluate_danger_returns_findings_and_records_blocked_attempt(client, monkeypatch):
    _stub_audit(monkeypatch, AuditResult(
        state="danger", verdict="block",
        findings=[Finding("judge", "dangerous_instruction", "critical", "rm -rf", "destructive")],
    ))
    r = _evaluate(client, "Dangerous")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "danger"
    assert body["findings"][0]["category"] == "dangerous_instruction"
    db = SessionLocal()
    assert db.query(RpAiSkill).count() == 0             # never stored
    rec = db.query(RpAiSkillAuditRecord).one()          # blocked attempt recorded
    assert rec.state == "danger" and rec.skill_id is None


def test_evaluate_suspect_returns_findings_without_storing(client, monkeypatch):
    _stub_audit(monkeypatch, AuditResult(
        state="suspect", verdict="flag",
        findings=[Finding("judge", "malicious_artifact", "medium", "app.zip", "bundles code")],
    ))
    r = _evaluate(client, "Sus")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "suspect" and body["findings"]
    db = SessionLocal()
    assert db.query(RpAiSkill).count() == 0
    assert db.query(RpAiSkillAuditRecord).count() == 0  # only danger is recorded at evaluate


def test_upload_response_carries_audit_outcome(client, monkeypatch):
    _stub_audit(monkeypatch, AuditResult(state="pass", verdict="pass"))
    _stub_store(monkeypatch)
    body = _upload(client, "Good").json()
    assert body["auditState"] == "pass"
    assert body["reference"]
    assert body["findings"] == []


def test_upload_suspect_response_carries_state_findings_and_mr(client, monkeypatch):
    _stub_audit(monkeypatch, AuditResult(
        state="suspect", verdict="flag",
        findings=[Finding("judge", "malicious_artifact", "medium", "app.zip", "bundles code")],
    ))
    _stub_store(monkeypatch)
    monkeypatch.setattr(upload_mod.gitlab_mr, "review_suspect", lambda **kw: "http://mr/9")
    body = _upload(client, "Sus").json()
    assert body["auditState"] == "suspect"
    assert body["mrUrl"] == "http://mr/9"
    assert body["findings"][0]["category"] == "malicious_artifact"
