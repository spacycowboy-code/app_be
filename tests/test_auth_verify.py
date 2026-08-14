"""The auth gate as seen by nginx: ``GET /api/auth/verify``.

Unlike the rest of the suite (which runs with ``AUTH_ENABLED=false``, see
``conftest``), these tests turn the site-wide JWT gate ON, because the whole
point of this endpoint is what it does with a token. It is what decides whether
a proxied tool — EriDoc, under ``/api/eridoc/`` — sees a request at all, and
which signum it is told the caller is, so both the reject and the identity path
need to be pinned down.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services.auth_tokens import create_token

SECRET = "verify-endpoint-test-secret"


@pytest.fixture
def auth_client(monkeypatch):
    """A client with the site-wide JWT gate enabled and a known signing key."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _token(eid: str, name: str | None = None) -> str:
    token, _ = create_token(eid, name)
    return token


def test_valid_session_returns_204_and_the_eid_header(auth_client):
    resp = auth_client.get(
        "/api/auth/verify",
        headers={"Authorization": f"Bearer {_token('esomeone')}"},
    )
    assert resp.status_code == 204
    # Uppercased so the identity handed to the proxied tool is stable, and no
    # body: nginx's auth_request only reads the status code and headers.
    assert resp.headers["X-Auth-Eid"] == "ESOMEONE"
    assert resp.content == b""


def test_non_ascii_display_name_does_not_reach_the_header(auth_client):
    """Only the EID is exported. A display name may be non-ASCII, and an HTTP
    header cannot carry that (latin-1 on the wire), so it must stay out."""
    resp = auth_client.get(
        "/api/auth/verify",
        headers={"Authorization": f"Bearer {_token('esomeone', '苗某某')}"},
    )
    assert resp.status_code == 204
    assert resp.headers["X-Auth-Eid"] == "ESOMEONE"
    assert all("苗" not in v for v in resp.headers.values())


def test_missing_token_is_401(auth_client):
    assert auth_client.get("/api/auth/verify").status_code == 401


def test_token_signed_with_another_key_is_401(auth_client, monkeypatch):
    good = _token("esomeone")
    monkeypatch.setenv("AUTH_JWT_SECRET", "a-different-secret")
    get_settings.cache_clear()
    assert auth_client.get(
        "/api/auth/verify", headers={"Authorization": f"Bearer {good}"}
    ).status_code == 401


def test_gate_disabled_means_401_not_anonymous(monkeypatch):
    """Fail closed: with auth off the gate stops establishing an identity, so
    verify must refuse rather than let nginx forward a nameless request."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert TestClient(app).get("/api/auth/verify").status_code == 401
    finally:
        get_settings.cache_clear()
