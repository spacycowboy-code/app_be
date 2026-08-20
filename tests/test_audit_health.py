"""The audit's own failure rate has to be a number, not a complaint.

Every rule in this gate fails closed, so each failure of the audit itself is paid for by a
reviewer's attention. One is an incident; a steady stream is our outage — and on a single record
the two are indistinguishable. Before this, the only way anyone noticed was by hand: the
refactor's 10-out-of-46 failure rate was found by diffing two corpus runs, and the empty review
MRs were found because a person asked what they meant.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.services import audit_engine
from app.services.skill_audit import gate
from app.services.audit_engine import AuditResult
from app.services.auth_tokens import create_token


@pytest.fixture()
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "health-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    return TestClient(app)


def _auth() -> dict:
    token, _ = create_token("EYINCNG", display_name="Terry Yin")
    return {"Authorization": f"Bearer {token}"}


def _seed(ref: str, **kw) -> None:
    with SessionLocal() as db:
        gate.record(db, ref, AuditResult(**kw), skill_id=None, skill_name="s",
                    uploader_eid="EYINCNG")
        db.commit()


def _health(client, days: int | None = None) -> dict:
    path = "/api/audit/health" + (f"?days={days}" if days else "")
    resp = client.get(path, headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestTheNumbersAreSeparable:
    def test_platform_failures_are_counted_apart_from_real_findings(self, client):
        _seed("h1", state="suspect", reason="r", coverage=audit_engine.COVERAGE_NONE,
              cause="scanner_timeout")
        _seed("h2", state="suspect", reason="r", coverage=audit_engine.COVERAGE_PARTIAL,
              cause="layer_failed")
        _seed("h3", state="suspect", reason="r", coverage=audit_engine.COVERAGE_NONE,
              cause="bundle_unloadable")
        _seed("h4", state="suspect", reason="r", coverage=audit_engine.COVERAGE_COMPLETE)
        _seed("h5", state="pass", coverage=audit_engine.COVERAGE_COMPLETE)

        body = _health(client)
        assert body["total"] == 5
        assert body["platformFailures"] == 2, body
        assert body["platformFailureRate"] == round(2 / 5, 4)
        assert body["byCause"]["bundle_unloadable"] == 1
        assert body["byState"]["suspect"] == 4
        assert body["byState"]["pass"] == 1

    def test_not_examined_counts_only_none_not_partial(self, client):
        """Partial means something was examined; folding the two together would overstate the
        damage and hide the difference the reviewer cares about."""
        _seed("n1", state="suspect", reason="r", coverage=audit_engine.COVERAGE_NONE,
              cause="bundle_unloadable")
        _seed("n2", state="suspect", reason="r", coverage=audit_engine.COVERAGE_PARTIAL,
              cause="bundle_unreadable_member")
        body = _health(client)
        assert body["notExamined"] == 1
        assert body["byCoverage"][audit_engine.COVERAGE_PARTIAL] == 1

    def test_records_from_before_the_columns_existed_are_counted_separately(self, client):
        """Folding them into a bucket would invent data. A growing number here means the
        measurement itself is degrading, which is worth seeing."""
        _seed("old", state="suspect", reason="审计没有产出结果:timed out after 900s",
              coverage="", cause=None)
        body = _health(client)
        assert body["unclassified"] == 1
        assert body["platformFailures"] == 0, "an unclassified record must not be guessed at"

    def test_an_empty_window_does_not_divide_by_zero(self, client):
        body = _health(client)
        assert body["total"] == 0
        assert body["platformFailureRate"] == 0.0


class TestTheEndpointItself:
    def test_it_requires_a_session(self, client):
        assert client.get("/api/audit/health").status_code == 401

    def test_the_window_is_bounded(self, client):
        assert client.get("/api/audit/health?days=0", headers=_auth()).status_code == 422
        assert client.get("/api/audit/health?days=91", headers=_auth()).status_code == 422
        assert client.get("/api/audit/health?days=90", headers=_auth()).status_code == 200

    def test_every_cause_the_verdict_can_produce_is_reportable(self, client):
        """Guards the seam: a cause with no owner would silently drop out of the rate."""
        for i, cause in enumerate(sorted({
                "bundle_unloadable", "bundle_unreadable_member",
                "bundle_too_big", "scanner_timeout",
                "scanner_broken", "layer_failed"})):
            _seed(f"all-{i}", state="suspect", reason="r",
                  coverage=audit_engine.COVERAGE_PARTIAL, cause=cause)
        body = _health(client)
        assert len(body["byCause"]) == 6, body["byCause"]
        assert body["platformFailures"] == 3, "timeout, broken, layer_failed"
