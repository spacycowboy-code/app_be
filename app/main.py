"""FastAPI application entry point."""
import logging
import re

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.version import __version__
from app.services.auth_tokens import TokenError, decode_token
from app.services.skill_audit.types import BundleTooLarge
from app.routers import (
    audit,
    auth,
    categories,
    comments,
    favorites,
    market,
    market_favorites,
    reviews,
    skill_import,
    skills,
    upload,
    users,
)

settings = get_settings()

# Without a handler on the root logger, Python's last-resort handler only emits
# WARNING and above, so successful logins (INFO) would never reach the container
# log — and a login audit trail that only records failures is half a trail.
# uvicorn configures its own loggers; this covers ours.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title=settings.app_name, version=__version__)

@app.exception_handler(BundleTooLarge)
async def _bundle_too_large(_request: Request, exc: BundleTooLarge) -> JSONResponse:
    """A bundle we will not unpack is an input problem, answered as one.

    Reported where the uploader can act on it, with the reason. It is not an audit
    verdict — nothing was examined — so no audit record is written.
    """
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        content={"detail": str(exc)})


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Paths reachable without a session. Besides login/health, these two carry
# their OWN token (webhook / CI) and are called by non-browser clients that
# have no JWT, so the site-wide JWT gate must let them through to their own
# auth check.
AUTH_WHITELIST = {
    "/health",
    "/api/version",
    "/api/auth/login",
    "/api/audit/reconcile",       # GitLab webhook — audit_webhook_token
    "/api/skills/import-official",  # official-skill sync CI — skill_import_token
}


# Paths the middleware lets through **without deciding anything**: the handler
# performs the auth check itself, because the answer depends on data rather than
# on the path. Today that is only the market download endpoint, where a per-item
# flag says whether that one project may be fetched without logging in — an agent
# installing a tool on someone's machine has no session, and having it collect the
# user's domain password is not an acceptable alternative.
#
# Anything listed here MUST enforce auth itself for the non-public case. A path
# added here and then left unchecked is an open endpoint, and it will not look
# like one from either side: the middleware sees a deliberate entry, and the
# handler looks like every other handler.
AUTH_DEFERRED_PATTERNS = (
    re.compile(r"^/api/market/[^/]+/download$"),
)


def _auth_deferred(path: str) -> bool:
    """Whether ``path`` decides its own auth (see :data:`AUTH_DEFERRED_PATTERNS`)."""
    return any(pattern.match(path) for pattern in AUTH_DEFERRED_PATTERNS)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Site-wide auth: any /api request (bar the whitelist) needs a valid JWT.

    Sets ``request.state.eid`` / ``request.state.display_name`` from the token
    so handlers can trust the caller's identity instead of a client-supplied
    field. CORS preflight (OPTIONS) and non-/api paths pass through.
    """
    cfg = get_settings()
    path = request.url.path
    if (
        not cfg.auth_enabled
        or request.method == "OPTIONS"
        or not path.startswith("/api/")
        or path in AUTH_WHITELIST
        or _auth_deferred(path)
    ):
        return await call_next(request)

    authz = request.headers.get("Authorization", "")
    token = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
    try:
        claims = decode_token(token)
    except TokenError:
        return JSONResponse(
            status_code=401,
            content={
                "detail": "Not signed in, or your session has expired. "
                          "Please sign in again."
            },
        )
    request.state.eid = claims.get("sub")
    request.state.display_name = claims.get("name") or None
    return await call_next(request)


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Turn DB constraint violations into a clean 400 rather than a 500."""
    return JSONResponse(
        status_code=400,
        content={"detail": "Database constraint violation", "error": str(exc.orig)},
    )


app.include_router(users.router)
app.include_router(auth.router)
app.include_router(favorites.router)
app.include_router(market_favorites.router)
app.include_router(skills.router)
app.include_router(upload.router)
app.include_router(audit.router)
app.include_router(skill_import.router)
app.include_router(reviews.router)
app.include_router(comments.router)
app.include_router(categories.router)
app.include_router(market.router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/version", tags=["health"])
def version() -> dict[str, str]:
    """Backend version (semantic) + the deployed build's git commit."""
    return {
        "name": settings.app_name,
        "version": __version__,
        "commit": settings.app_git_commit,
    }
