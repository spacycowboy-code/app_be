"""JWT session tokens.

The backend signs a short-lived HMAC (HS256) token after a successful LDAP
login. The token carries the user's EID (subject) and display name. Every
protected request must present it as ``Authorization: Bearer <token>``; the
gate in ``app.main`` validates it and sets ``request.state.eid``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings


class TokenError(Exception):
    """Token missing, malformed, expired, or signature invalid."""


def create_token(eid: str, display_name: str | None = None) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)`` for the given EID.

    Raises ``RuntimeError`` if no signing secret is configured — a token that
    can't be verified is worse than no auth, so fail loudly.
    """
    settings = get_settings()
    if not settings.auth_jwt_secret:
        raise RuntimeError("auth_jwt_secret is not configured")
    ttl = timedelta(hours=settings.auth_token_ttl_hours)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": eid,
        "name": display_name or "",
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    token = jwt.encode(payload, settings.auth_jwt_secret, algorithm=settings.auth_jwt_algorithm)
    return token, int(ttl.total_seconds())


def decode_token(token: str) -> dict:
    """Validate a token and return its claims, or raise ``TokenError``."""
    settings = get_settings()
    if not settings.auth_jwt_secret:
        raise TokenError("auth not configured")
    if not token:
        raise TokenError("missing token")
    try:
        return jwt.decode(
            token, settings.auth_jwt_secret, algorithms=[settings.auth_jwt_algorithm]
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
