"""What the person who uploaded the skill is told.

The audit refusing something is only useful if the author can act on it. Two records in
production show the cost of not saying why: the same skill uploaded twice, refused both times
for the same malformed frontmatter, because the response said only "needs review".
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.routers import upload as upload_mod
from app.services.auth_tokens import create_token
from app.services.skill_audit.types import AuditResult, Finding
from app.services.skill_storage import StoreResult

SKILL_MD = "---\nname: T\ndescription: d\n---\n# T\nbody\n"
EID = "EYINCNG"
NO_REPORT = "审计没有产出结果:Error loading skill: SKILL.md missing required field: name"


@pytest.fixture()
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "reason-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    monkeypatch.setattr(
        upload_mod.skill_storage, "store",
        lambda **kw: StoreResult("public/x", "1.0.0", "abc123", None, None, False),
    )
    return TestClient(app)


def _auth() -> dict:
    token, _ = create_token(EID, display_name="Terry Yin")
    return {"Authorization": f"Bearer {token}"}


def _upload(client, name):
    return client.post(
        "/api/skills/upload",
        data={"eid": EID, "name": name, "categoryId": "public"},
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
        headers=_auth(),
    )


def _evaluate(client, name):
    return client.post(
        "/api/skills/evaluate",
        data={"eid": EID, "name": name},
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
        headers=_auth(),
    )


class TestTheAuthorIsToldWhy:
    def test_a_refused_upload_says_why_not_just_that_it_was_refused(self, client, monkeypatch):
        monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: AuditResult(
            state="danger", reason="两层一致的 critical:command_injection(LLM_COMMAND_INJECTION)",
            findings=[Finding(source="static", category="command_injection",
                              severity="critical", rule_id="LLM_COMMAND_INJECTION")]))
        resp = _upload(client, "refused-with-reason")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "command_injection" in detail, detail
        assert "参考编号" in detail, "the reference still has to be there for support"

    def test_a_suspect_upload_carries_the_reason_in_the_response(self, client, monkeypatch):
        """This is the case that produced two identical uploads: suspect, no findings."""
        monkeypatch.setattr(upload_mod, "audit_skill",
                            lambda **kw: AuditResult(state="suspect", reason=NO_REPORT))
        resp = _upload(client, "suspect-with-reason")
        assert resp.status_code == 201
        body = resp.json()
        assert body["auditState"] == "suspect"
        assert body["reason"] == NO_REPORT, body

    def test_a_passing_upload_has_no_reason_to_show(self, client, monkeypatch):
        monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: AuditResult(state="pass"))
        body = _upload(client, "clean-upload").json()
        assert body["auditState"] == "pass"
        assert not body.get("reason")

    def test_evaluate_already_carried_it_and_still_does(self, client, monkeypatch):
        monkeypatch.setattr(upload_mod, "audit_skill",
                            lambda **kw: AuditResult(state="suspect", reason=NO_REPORT))
        body = _evaluate(client, "evaluate-reason").json()
        assert body["state"] == "suspect"
        assert body["reason"] == NO_REPORT


class TestTheTwoFieldsReachTheClient:
    """The wizard renders its own wording from these, so an API that omits them silently
    downgrades the author back to a sentence written for a reviewer. Found exactly that way:
    the frontend was reading fields the responses did not carry."""

    def test_evaluate_returns_coverage_and_cause(self, client, monkeypatch):
        monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: AuditResult(
            state="suspect", reason=NO_REPORT, coverage="none", cause="bundle_unloadable"))
        body = _evaluate(client, "cov-evaluate").json()
        assert body["coverage"] == "none"
        assert body["cause"] == "bundle_unloadable"

    def test_upload_returns_coverage_and_cause(self, client, monkeypatch):
        monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: AuditResult(
            state="suspect", reason=NO_REPORT, coverage="partial", cause="layer_failed"))
        body = _upload(client, "cov-upload").json()
        assert body["coverage"] == "partial"
        assert body["cause"] == "layer_failed"

    def test_a_clean_pass_carries_complete_and_no_cause(self, client, monkeypatch):
        monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: AuditResult(state="pass"))
        body = _evaluate(client, "cov-clean").json()
        assert body["coverage"] == "complete"
        assert body["cause"] is None
