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


# --- the projection: one lifecycle gate, rebuilt from the audit facts --------

def test_pass_is_approved_and_suspect_waits_for_review(client, monkeypatch):
    _stub_store(monkeypatch)
    _stub_audit(monkeypatch, AuditResult(state="pass", verdict="pass"))
    _upload(client, "Good")
    _stub_audit(monkeypatch, AuditResult(state="suspect", verdict="flag",
                                         findings=[Finding("judge", "malicious_artifact", "medium")]))
    monkeypatch.setattr(upload_mod.gitlab_mr, "review_suspect", lambda **kw: "http://mr/7")
    _upload(client, "Sus")

    db = SessionLocal()
    assert db.get(RpAiSkill, "good").status == "approved"
    assert db.get(RpAiSkill, "sus").status == "pending"
    public = [x["id"] for x in client.get("/api/skills").json()]
    assert "good" in public and "sus" not in public


def test_reconcile_publishes_a_skill_once_its_review_is_merged(client, monkeypatch):
    from app.services.skill_audit import reconcile as rec

    _stub_store(monkeypatch)
    _stub_audit(monkeypatch, AuditResult(state="suspect", verdict="flag",
                                         findings=[Finding("judge", "malicious_artifact", "medium")]))
    monkeypatch.setattr(upload_mod.gitlab_mr, "review_suspect", lambda **kw: "http://gl/-/merge_requests/7")
    _upload(client, "Sus")
    assert "sus" not in [x["id"] for x in client.get("/api/skills").json()]

    monkeypatch.setattr(rec.gitlab_mr, "mr_state", lambda url: "merged")
    db = SessionLocal()
    result = rec.reconcile_all(db)
    assert result["changed"] == {"sus": "approved"}
    assert "sus" in [x["id"] for x in client.get("/api/skills").json()]


def test_reconcile_marks_a_closed_review_rejected_and_keeps_it_hidden(client, monkeypatch):
    from app.services.skill_audit import reconcile as rec

    _stub_store(monkeypatch)
    _stub_audit(monkeypatch, AuditResult(state="suspect", verdict="flag",
                                         findings=[Finding("judge", "prompt_injection", "medium")]))
    monkeypatch.setattr(upload_mod.gitlab_mr, "review_suspect", lambda **kw: "http://gl/-/merge_requests/9")
    _upload(client, "Sus")

    monkeypatch.setattr(rec.gitlab_mr, "mr_state", lambda url: "closed")
    assert rec.reconcile_all(SessionLocal())["changed"] == {"sus": "rejected"}
    assert "sus" not in [x["id"] for x in client.get("/api/skills").json()]


def test_reconcile_leaves_it_pending_when_the_review_state_is_unknown(client, monkeypatch):
    from app.services.skill_audit import reconcile as rec

    _stub_store(monkeypatch)
    _stub_audit(monkeypatch, AuditResult(state="suspect", verdict="flag",
                                         findings=[Finding("judge", "malicious_artifact", "medium")]))
    monkeypatch.setattr(upload_mod.gitlab_mr, "review_suspect", lambda **kw: "http://gl/-/merge_requests/11")
    _upload(client, "Sus")

    monkeypatch.setattr(rec.gitlab_mr, "mr_state", lambda url: None)   # no token / network
    assert rec.reconcile_all(SessionLocal())["changed"] == {}          # fails closed
    assert SessionLocal().get(RpAiSkill, "sus").status == "pending"


def test_projection_is_rebuildable_by_running_it_twice(client, monkeypatch):
    from app.services.skill_audit import reconcile as rec

    _stub_store(monkeypatch)
    _stub_audit(monkeypatch, AuditResult(state="suspect", verdict="flag",
                                         findings=[Finding("judge", "malicious_artifact", "medium")]))
    monkeypatch.setattr(upload_mod.gitlab_mr, "review_suspect", lambda **kw: "http://gl/-/merge_requests/13")
    _upload(client, "Sus")
    monkeypatch.setattr(rec.gitlab_mr, "mr_state", lambda url: "merged")
    first = rec.reconcile_all(SessionLocal())["changed"]
    second = rec.reconcile_all(SessionLocal())["changed"]      # idempotent
    assert first == {"sus": "approved"} and second == {}


# --- the reconcile trigger: one function, two triggers ----------------------

def test_webhook_trigger_needs_the_secret_and_then_reconciles(client, monkeypatch):
    from app.routers import audit as audit_mod
    from app.services.skill_audit import reconcile as rec

    monkeypatch.setattr(audit_mod.settings, "audit_webhook_token", "", raising=False)
    assert client.post("/api/audit/reconcile").status_code == 403      # disabled

    monkeypatch.setattr(audit_mod.settings, "audit_webhook_token", "s3cret", raising=False)
    assert client.post("/api/audit/reconcile").status_code == 401      # wrong token

    _stub_store(monkeypatch)
    _stub_audit(monkeypatch, AuditResult(state="suspect", verdict="flag",
                                         findings=[Finding("judge", "malicious_artifact", "medium")]))
    monkeypatch.setattr(upload_mod.gitlab_mr, "review_suspect",
                        lambda **kw: "http://gl/-/merge_requests/21")
    _upload(client, "Sus")
    monkeypatch.setattr(rec.gitlab_mr, "mr_state", lambda url: "merged")

    r = client.post("/api/audit/reconcile", headers={"X-Gitlab-Token": "s3cret"})
    assert r.status_code == 200
    assert r.json()["changed"] == {"sus": "approved"}                  # same convergence
    assert "sus" in [x["id"] for x in client.get("/api/skills").json()]
