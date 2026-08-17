"""Scoped (single-path) token behaviour.

These tests exist because a scoped token is signed with the *same* secret as a
session token. That means the signature alone cannot tell them apart, and every
guarantee this feature makes rests on ``check_token_scope`` being applied. The
cases below are the ones whose failure would be silent in production: the token
would still verify, and the request would still succeed.
"""
from __future__ import annotations

import time

import jwt
import pytest

from app.config import get_settings
from app.services.auth_tokens import (
    TOKEN_TYPE_SCOPED,
    TokenError,
    check_token_scope,
    create_scoped_token,
    create_token,
    decode_token,
)

DOWNLOAD_PATH = "/api/market/gl-141618/download"


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    """Give the module a secret without depending on the deployment config."""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_jwt_secret", "unit-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    monkeypatch.setattr(settings, "scoped_token_ttl_minutes", 10, raising=False)
    yield


def test_scoped_token_allows_only_its_own_path_and_method():
    token, _ = create_scoped_token("EZHGMIO", DOWNLOAD_PATH, method="POST")
    claims = decode_token(token)

    check_token_scope(claims, DOWNLOAD_PATH, "POST")  # the one it was issued for

    # A different item. This is the case that matters: the portal's own GitLab
    # token can read far more repositories than any single caller should see.
    with pytest.raises(TokenError):
        check_token_scope(claims, "/api/market/gl-999999/download", "POST")

    with pytest.raises(TokenError):
        check_token_scope(claims, DOWNLOAD_PATH, "GET")


def test_scoped_token_is_not_a_session_token():
    """The whole point: it must not work as a general bearer token.

    Signed with the same key, so ``decode_token`` accepts it. Only the scope
    check stops it from reaching, say, the delete endpoint.
    """
    token, _ = create_scoped_token("EZHGMIO", DOWNLOAD_PATH)
    claims = decode_token(token)
    assert claims["typ"] == TOKEN_TYPE_SCOPED

    for path, method in [
        ("/api/market", "GET"),
        ("/api/market/gl-141618", "DELETE"),
        ("/api/market/gl-141618/install-token", "POST"),  # cannot mint more
        ("/api/skills/upload", "POST"),
    ]:
        with pytest.raises(TokenError):
            check_token_scope(claims, path, method)


def test_session_token_still_works_everywhere():
    token, _ = create_token("EZHGMIO", "Miao Zhang M")
    claims = decode_token(token)
    check_token_scope(claims, "/api/market", "GET")
    check_token_scope(claims, "/api/skills/upload", "POST")


def test_token_without_typ_is_treated_as_a_session():
    """Tokens issued before ``typ`` existed must keep working.

    Requiring ``typ == "session"`` would log out every signed-in user the moment
    this deploys. Reading a missing ``typ`` as a session is safe because a scoped
    token always carries ``typ`` explicitly.
    """
    settings = get_settings()
    legacy = jwt.encode(
        {"sub": "EZHGMIO", "name": "", "exp": int(time.time()) + 3600},
        settings.auth_jwt_secret,
        algorithm=settings.auth_jwt_algorithm,
    )
    check_token_scope(decode_token(legacy), "/api/market", "GET")


def test_unknown_token_type_is_rejected():
    """An unrecognised ``typ`` must fail closed, not fall through as a session."""
    settings = get_settings()
    weird = jwt.encode(
        {"sub": "EZHGMIO", "typ": "something-new", "exp": int(time.time()) + 3600},
        settings.auth_jwt_secret,
        algorithm=settings.auth_jwt_algorithm,
    )
    with pytest.raises(TokenError):
        check_token_scope(decode_token(weird), "/api/market", "GET")


def test_repointing_the_path_breaks_the_signature():
    """The scope is covered by the signature, so the holder cannot edit it."""
    settings = get_settings()
    token, _ = create_scoped_token("EZHGMIO", DOWNLOAD_PATH)
    header_b64, payload_b64, signature = token.split(".")

    tampered_payload = jwt.utils.base64url_encode(
        jwt.utils.base64url_decode(payload_b64).replace(b"gl-141618", b"gl-999999")
    ).decode()
    # Same signature, different claims.
    with pytest.raises(TokenError):
        decode_token(f"{header_b64}.{tampered_payload}.{signature}")

    _ = settings  # fixture applied above; kept for readability of intent


def test_expired_scoped_token_is_rejected():
    token, expires_in = create_scoped_token(
        "EZHGMIO", DOWNLOAD_PATH, ttl_minutes=-1
    )
    assert expires_in < 0
    with pytest.raises(TokenError):
        decode_token(token)


def test_scoped_token_ttl_comes_from_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "scoped_token_ttl_minutes", 3, raising=False)
    _, expires_in = create_scoped_token("EZHGMIO", DOWNLOAD_PATH)
    assert expires_in == 180
