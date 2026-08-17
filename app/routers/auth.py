"""Authentication endpoints: LDAP login -> JWT session, and current-user probe.

``POST /api/auth/login`` verifies the user's personal credentials against
Active Directory (via the service bind account), upserts the portal user row,
and returns a signed JWT. ``GET /api/auth/me`` echoes the identity carried by a
valid token (the auth gate in ``app.main`` has already validated it).
``GET /api/auth/verify`` is the same check reduced to a status code plus an
identity header, for nginx's ``auth_request`` (see ``verify`` below).
"""
from __future__ import annotations

import logging
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.user import RpAiPortal
from app.services import ldap_auth
from app.services.auth_tokens import create_token

router = APIRouter(prefix="/api/auth", tags=["auth"])

logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    username: str = Field(..., description="Corporate login name (sAMAccountName)")
    # SecretStr: masked in logs/repr/errors and rendered as a password field in
    # the OpenAPI docs. The raw value (needed for the LDAP bind) is read via
    # .get_secret_value() only inside the login handler below.
    password: SecretStr = Field(..., description="Corporate password — verified via LDAP")


class LoginResponse(BaseModel):
    token: str
    tokenType: str = "Bearer"
    expiresIn: int
    eid: str
    displayName: str | None = None


class MeResponse(BaseModel):
    eid: str
    displayName: str | None = None


def _upsert_user(db: Session, user: ldap_auth.LdapUser) -> None:
    """Create or refresh the portal user row from LDAP attributes."""
    row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == user.eid))
    if row is None:
        row = RpAiPortal(eid=user.eid)
        db.add(row)
    if user.display_name:
        row.display_name = user.display_name[:128]
    if user.department:
        row.department = user.department[:64]
    db.commit()


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest, request: Request, db: Session = Depends(get_db)
) -> LoginResponse:
    settings = get_settings()
    if not settings.auth_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "登录功能未启用")
    if not settings.auth_jwt_secret:
        # Misconfiguration: refuse rather than issue unverifiable tokens.
        logger.error("Login refused: auth_jwt_secret is not configured")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "认证服务未正确配置")

    # Break-glass local admin: authenticates WITHOUT LDAP. Its EID is the admin
    # EID, so the session has admin rights everywhere. Username match is
    # case-insensitive; the password is compared in constant time.
    admin_user = settings.local_admin_username.strip()
    if (
        settings.local_admin_enabled
        and admin_user
        and payload.username.strip().lower() == admin_user.lower()
        and secrets.compare_digest(payload.password.get_secret_value(), settings.local_admin_password)
    ):
        admin = ldap_auth.LdapUser(
            eid=settings.audit_admin_eid.upper(),
            display_name=settings.local_admin_display,
            department=None,
            dn="",
        )
        _upsert_user(db, admin)
        token, expires_in = create_token(admin.eid, admin.display_name)
        return LoginResponse(
            token=token, expiresIn=expires_in, eid=admin.eid, displayName=admin.display_name
        )

    try:
        user = ldap_auth.authenticate(payload.username, payload.password.get_secret_value())
    except ldap_auth.LdapAuthError as exc:
        # One log line per rejected login, carrying the machine-readable reason
        # and the caller's address. This is the record that was missing when
        # "some people get 401" had to be diagnosed from user reports alone.
        logger.warning(
            "Login rejected: user=%r reason=%s retryable=%s client=%s",
            payload.username,
            exc.reason,
            exc.retryable,
            request.client.host if request.client else "?",
        )
        if exc.retryable:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    _upsert_user(db, user)
    token, expires_in = create_token(user.eid, user.display_name)
    return LoginResponse(
        token=token, expiresIn=expires_in, eid=user.eid, displayName=user.display_name
    )


@router.get("/me", response_model=MeResponse)
def me(request: Request) -> MeResponse:
    """Return the identity carried by the (already-validated) bearer token."""
    eid = getattr(request.state, "eid", None)
    if not eid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    return MeResponse(eid=eid, displayName=getattr(request.state, "display_name", None))


#: An EID is a signum: ASCII only. Anything else is refused rather than copied
#: into a response header, because that header is what nginx forwards to another
#: service as the caller's identity (see ``verify``).
_EID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@router.get("/verify", status_code=status.HTTP_204_NO_CONTENT)
def verify(request: Request) -> Response:
    """Session check for nginx's ``auth_request``: status code + identity header.

    The portal's frontend nginx reverse-proxies tools that have no idea what a
    portal JWT is (currently EriDoc, under ``/api/eridoc/``). It sends every
    such request here first: 204 means the caller holds a valid session, and
    ``X-Auth-Eid`` tells nginx whose it is, so it can inject that signum as the
    tool's ``X-Forwarded-User``. 401 makes nginx refuse the request outright.

    Deliberately not in ``AUTH_WHITELIST``: the site-wide gate in ``app.main``
    is what validates the token, and this handler only reports the result. That
    also makes the failure mode safe — with ``auth_enabled=false`` the gate
    stops setting ``request.state.eid``, so this returns 401 and the proxied
    tool becomes unreachable instead of anonymous.

    ``Response`` is returned directly rather than via ``response_model``: a 204
    must not carry a body, and the whole point here is the header.
    """
    eid = str(getattr(request.state, "eid", "") or "").strip()
    if not eid or not _EID_RE.match(eid):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    return Response(status_code=status.HTTP_204_NO_CONTENT,
                    headers={"X-Auth-Eid": eid.upper()})
