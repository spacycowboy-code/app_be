"""FastAPI application entry point."""
import logging

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.version import __version__
from app.services.auth_tokens import TokenError, check_token_scope, decode_token
from app.services.audit_engine import BundleTooLarge
from app.routers import (
    audit,
    auth,
    categories,
    comments,
    favorites,
    login_stats,
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


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Site-wide auth: any /api request (bar the whitelist) needs a valid JWT.

    Sets ``request.state.eid`` / ``request.state.display_name`` from the token
    so handlers can trust the caller's identity instead of a client-supplied
    field. CORS preflight (OPTIONS) and non-/api paths pass through.

    Two checks, and the second one is easy to overlook: a valid signature only
    proves *we* issued the token and it has not expired. It does not say the
    token may be used **here**. ``check_token_scope`` answers that, because some
    tokens are deliberately restricted to a single endpoint (see
    ``create_scoped_token``). Skipping it would silently promote a token meant
    for one download into a full session.

    Doing it in the gate rather than per handler is the point: the restriction
    holds for every route, including ones added later by someone who has never
    read this file.
    """
    cfg = get_settings()
    path = request.url.path
    if (
        not cfg.auth_enabled
        or request.method == "OPTIONS"
        or not path.startswith("/api/")
        or path in AUTH_WHITELIST
    ):
        return await call_next(request)

    authz = request.headers.get("Authorization", "")
    token = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
    try:
        claims = decode_token(token)
        check_token_scope(claims, path, request.method)
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
app.include_router(login_stats.router)
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
