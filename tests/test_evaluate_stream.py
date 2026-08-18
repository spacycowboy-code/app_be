"""The streamed evaluate must always end in a way the client can act on.

The status line goes out with the first event, so nothing after that can become an HTTP
error. Every path therefore has to end in a ``result`` or an ``error`` event — a stream that
just stops looks identical to a slow audit, and the wizard would wait for a verdict that is
never coming.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.audit import RpAiSkillAuditRecord
from app.routers import upload as upload_mod
from app.services.auth_tokens import create_token
from app.services.skill_audit.types import AuditResult, Finding

SKILL_MD = "---\nname: T\ndescription: d\n---\n# T\nbody\n"
SIGNED_IN, CLAIMED = "EYINCNG", "EXINGEN"


@pytest.fixture()
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "stream-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    return TestClient(app)


def _auth() -> dict:
    token, _ = create_token(SIGNED_IN, display_name="Terry Yin")
    return {"Authorization": f"Bearer {token}"}


def _events(text: str) -> list[tuple[str, str]]:
    """Parse an SSE body into (event, data) pairs."""
    out = []
    for block in text.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name:
            out.append((name, data or ""))
    return out


def _post(client, name="streamed"):
    return client.post(
        "/api/skills/evaluate/stream",
        data={"eid": CLAIMED, "name": name},
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
        headers=_auth(),
    )


def _stub_audit(monkeypatch, fn):
    """Patch the audit as the stream router imported it."""
    from app.routers import upload_stream
    monkeypatch.setattr(upload_stream, "audit_skill", fn)


class TestTheStreamAlwaysConcludes:
    def test_stages_then_a_result(self, client, monkeypatch):
        def fake(**kw):
            on_progress = kw["on_progress"]
            for stage in ("semantic", "arbitration", "reporting"):
                on_progress(stage)
            return AuditResult(state="pass")

        _stub_audit(monkeypatch, fake)
        resp = _post(client)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers.get("x-accel-buffering") == "no", "a buffering proxy would hide this"
        events = _events(resp.text)
        names = [n for n, _d in events]
        assert names[0] == "stage"
        assert names[-1] == "result"
        stages = [d for n, d in events if n == "stage"]
        assert any("deterministic" in s for s in stages)
        assert any("semantic" in s for s in stages)
        assert any("arbitration" in s for s in stages)
        assert '"state": "pass"' in events[-1][1]

    def test_a_failing_audit_sends_an_error_event_not_silence(self, client, monkeypatch):
        """Without this the client cannot tell a crash from a slow scan."""
        def boom(**kw):
            raise RuntimeError("scanner exploded")

        _stub_audit(monkeypatch, boom)
        resp = _post(client)
        assert resp.status_code == 200      # the status line left before the failure
        names = [n for n, _d in _events(resp.text)]
        assert "error" in names, f"got {names}"
        assert "result" not in names

    def test_a_danger_verdict_is_recorded_under_the_session_identity(self, client, monkeypatch):
        def dangerous(**kw):
            return AuditResult(
                state="danger", reason="两层一致的 critical:command_injection(X)",
                findings=[Finding(source="static", category="command_injection",
                                  severity="critical", rule_id="X")],
            )

        _stub_audit(monkeypatch, dangerous)
        resp = _post(client, name="streamed-danger")
        assert resp.status_code == 200
        names = [n for n, _d in _events(resp.text)]
        assert names[-1] == "result"
        with SessionLocal() as db:
            rec = (db.query(RpAiSkillAuditRecord)
                     .filter(RpAiSkillAuditRecord.skill_name == "streamed-danger")
                     .order_by(RpAiSkillAuditRecord.id.desc()).first())
            assert rec is not None, "a blocked attempt must leave a record"
            assert rec.uploader_eid == SIGNED_IN, "the form said someone else"
            assert rec.state == "danger"

    def test_an_unauthenticated_stream_is_refused(self, client):
        resp = client.post(
            "/api/skills/evaluate/stream",
            data={"eid": CLAIMED, "name": "nope"},
            files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
        )
        assert resp.status_code == 401

    def test_the_plain_endpoint_still_exists(self, client, monkeypatch):
        """publish re-runs the audit and has no use for progress, and a client that cannot
        read a stream must keep an option."""
        monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: AuditResult(state="pass"))
        resp = client.post(
            "/api/skills/evaluate",
            data={"eid": CLAIMED, "name": "plain"},
            files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "pass"
