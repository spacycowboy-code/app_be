"""The recorded uploader is the signed-in caller, not a field the client filled in.

Before this, an upload's ``eid`` came from the multipart form. Anyone signed in could put a
colleague's EID there and have the skill, the review MR title and the audit record all
attributed to that colleague. The audit gate never looked at identity, so nothing was
bypassed — but attribution and the review trail were forgeable.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.audit import RpAiSkillAuditRecord
from app.models.skill import RpAiSkill
from app.routers import upload as upload_mod
from app.services.auth_tokens import create_token
from app.services.audit_engine import AuditResult
from app.services.skill_storage import StoreResult

SKILL_MD = "---\nname: T\ndescription: d\n---\n# T\nbody\n"

SIGNED_IN = "EYINCNG"          # who actually holds the session
CLAIMED = "EXINGEN"            # who the client says it is


@pytest.fixture()
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "actor-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    monkeypatch.setattr(
        upload_mod.skill_storage, "store",
        lambda **kw: StoreResult("public/x", "1.0.0", "abc123", None, None, False),
    )
    monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: AuditResult(state="pass"))
    return TestClient(app)


def _auth() -> dict:
    token, _ = create_token(SIGNED_IN, display_name="Terry Yin")
    return {"Authorization": f"Bearer {token}"}


def test_upload_records_the_signed_in_caller_not_the_claim(client):
    resp = client.post(
        "/api/skills/upload",
        data={"eid": CLAIMED, "name": "actor-upload", "categoryId": "public"},
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
        headers=_auth(),
    )
    assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        skill = db.query(RpAiSkill).filter(RpAiSkill.name == "actor-upload").one()
        assert skill.uploader_eid == SIGNED_IN
        assert skill.uploader_eid != CLAIMED


def test_evaluate_records_the_signed_in_caller_on_a_blocked_attempt(client, monkeypatch):
    """The danger record exists for traceability, so its EID is the point of it."""
    monkeypatch.setattr(
        upload_mod, "audit_skill",
        lambda **kw: AuditResult(state="danger", reason="test"),
    )
    resp = client.post(
        "/api/skills/evaluate",
        data={"eid": CLAIMED, "name": "actor-evaluate"},
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        rec = (db.query(RpAiSkillAuditRecord)
                 .order_by(RpAiSkillAuditRecord.id.desc()).first())
        assert rec is not None
        assert rec.uploader_eid == SIGNED_IN


def test_an_unauthenticated_upload_is_refused_by_the_gate(client):
    """Not a new rule — the site-wide gate already does this. Asserted here because the
    identity fix relies on it: without a token there is no verified EID to record."""
    resp = client.post(
        "/api/skills/upload",
        data={"eid": CLAIMED, "name": "no-token", "categoryId": "public"},
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
    )
    assert resp.status_code == 401
