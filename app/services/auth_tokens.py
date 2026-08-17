"""JWT session tokens.

The backend signs a short-lived HMAC (HS256) token after a successful LDAP
login. The token carries the user's EID (subject) and display name. Every
protected request must present it as ``Authorization: Bearer <token>``; the
gate in ``app.main`` validates it and sets ``request.state.eid``.

Two kinds of token are signed with the same secret, told apart by the ``typ``
claim:

``session``
    What login returns. Good for every protected endpoint until it expires.

``scoped``
    Grants exactly **one** request path and method, for a few minutes. Used to
    hand an agent running on someone's machine the ability to fetch one archive
    without giving it a session — see ``create_scoped_token``.

Because both are signed with the same key, a valid signature says only "we
issued this, and it has not expired". It does **not** say the caller may reach
the path they are asking for. ``app.main.auth_gate`` is what enforces the
difference; nothing else in the codebase should call :func:`decode_token`
without deciding what the ``typ`` means for it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings


class TokenError(Exception):
    """Token missing, malformed, expired, or signature invalid."""


#: ``typ`` of a normal login session token. Tokens issued before this claim
#: existed have no ``typ`` at all, so a missing value is read as this — see
#: ``app.main.auth_gate``.
TOKEN_TYPE_SESSION = "session"

#: ``typ`` of a token restricted to a single path + method.
TOKEN_TYPE_SCOPED = "scoped"


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
        "typ": TOKEN_TYPE_SESSION,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    token = jwt.encode(payload, settings.auth_jwt_secret, algorithm=settings.auth_jwt_algorithm)
    return token, int(ttl.total_seconds())


def create_scoped_token(
    eid: str, path: str, method: str = "POST", ttl_minutes: int | None = None
) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)`` valid for exactly one request.

    ``path`` and ``method`` are recorded as claims, so they are covered by the
    signature: the holder cannot repoint the token at a different endpoint
    without invalidating it.

    **``path`` must be built by the server from data it already trusts, never
    taken from the caller.** A caller-supplied path would turn any logged-in
    user's request into "sign me a token for whatever endpoint I name", which is
    strictly worse than the session token they already hold, because the scoped
    token then travels outside the browser (into a shell, a log, an agent
    transcript) where a session token never goes.

    The short TTL is the containment: these tokens are meant to be pasted into a
    terminal or an agent prompt, i.e. into places that keep history.
    """
    settings = get_settings()
    if not settings.auth_jwt_secret:
        raise RuntimeError("auth_jwt_secret is not configured")
    minutes = settings.scoped_token_ttl_minutes if ttl_minutes is None else ttl_minutes
    ttl = timedelta(minutes=minutes)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": eid,
        "typ": TOKEN_TYPE_SCOPED,
        "path": path,
        "method": method.upper(),
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    token = jwt.encode(payload, settings.auth_jwt_secret, algorithm=settings.auth_jwt_algorithm)
    return token, int(ttl.total_seconds())


def decode_token(token: str) -> dict:
    """Validate a token and return its claims, or raise ``TokenError``.

    This answers exactly two questions: did we sign it, and is it still valid in
    time? It deliberately does **not** look at ``typ``, ``path`` or any other
    claim — PyJWT would not check those either. Callers must decide what the
    claims mean for the request at hand.
    """
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


def check_token_scope(claims: dict, path: str, method: str) -> None:
    """Raise ``TokenError`` unless ``claims`` may be used for this request.

    A ``session`` token (or a legacy token with no ``typ``) may be used
    anywhere. A ``scoped`` token may be used only for the exact path and method
    it was signed for.

    An unrecognised ``typ`` is rejected rather than waved through: if a future
    token kind is added and this function is not taught about it, the safe
    outcome is that it does not work yet.
    """
    typ = claims.get("typ") or TOKEN_TYPE_SESSION
    if typ == TOKEN_TYPE_SESSION:
        return
    if typ != TOKEN_TYPE_SCOPED:
        raise TokenError(f"unsupported token type: {typ!r}")
    if claims.get("path") != path:
        raise TokenError("token is not valid for this path")
    if (claims.get("method") or "").upper() != method.upper():
        raise TokenError("token is not valid for this method")

