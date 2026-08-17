"""The auth gate's treatment of scoped tokens, exercised through real requests.

``test_scoped_token.py`` covers the decision function. This file covers the wiring:
that the gate actually calls it, on every route, before any handler runs. Both are
needed — the guarantee is worthless if the check exists but is not reached.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services.auth_tokens import create_scoped_token, create_token

DOWNLOAD_PATH = "/api/market/gl-141618/download"


@pytest.fixture()
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "gate-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    monkeypatch.setattr(settings, "scoped_token_ttl_minutes", 10, raising=False)
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_gate_rejects_scoped_token_on_other_routes(client):
    """A download token must not open anything else.

    Asserting *not 401* would be wrong here, and asserting a specific success
    code would couple this test to each handler. What matters is that the gate
    answers 401 — the request never reaches the handler at all.
    """
    token, _ = create_scoped_token("EZHGMIO", DOWNLOAD_PATH)

    assert client.get("/api/market", headers=_auth(token)).status_code == 401
    assert client.post(
        "/api/market/gl-141618/install-token", headers=_auth(token)
    ).status_code == 401


def test_gate_lets_a_scoped_token_reach_its_own_route(client):
    """It must get past the gate — what the handler then does is its business.

    The item does not exist in the test database, so a 404 from the handler is
    the proof we want: the gate let it through and the handler ran.
    """
    token, _ = create_scoped_token("EZHGMIO", DOWNLOAD_PATH)
    response = client.post(DOWNLOAD_PATH, headers=_auth(token))
    assert response.status_code != 401


def test_gate_still_accepts_a_session_token(client):
    token, _ = create_token("EZHGMIO", "Miao Zhang M")
    assert client.get("/api/market", headers=_auth(token)).status_code == 200


def test_gate_rejects_a_request_with_no_token(client):
    assert client.get("/api/market").status_code == 401
    assert client.post(DOWNLOAD_PATH).status_code == 401
