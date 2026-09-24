"""FastAPI application entry point."""
import logging

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.repositories import skill_content
from app.version import __version__
from app.services.auth_tokens import TokenError, check_token_scope, decode_token
from app.services.audit_engine import BundleTooLarge
from app.routers import (
    admin_assets,
    agent_upload,
    api_errors as api_errors_router,
    audit,
    auth,
    categories,
    comments,
    departments,
    favorites,
    gerrit_sync,
    external_usage,
    ivc_sync,
    ivc_web_assets,
    login_stats,
    market,
    market_favorites,
    reviews,
    rsw_inventory,
    rdg,
    skill_import,
    skills,
    upload,
    usage,
    usage_export,
    usage_report,
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

logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name, version=__version__)

@app.exception_handler(skill_content.Conflict)
async def _content_conflict(_: Request, exc: skill_content.Conflict) -> JSONResponse:
    """同一个技能被并发改了。**不重试** —— 重试等于拿这次的内容盖掉对方刚写的那份。

    让调用方重新读一遍再决定。写不成的时候数据库那一行还没写(顺序是先仓库后数据库),
    所以这里返回错误不会留下"库里有、仓库没有"的状态。
    """
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "这个技能刚被其他人改过，请刷新后重新提交。"},
    )


@app.exception_handler(skill_content.ContentUnavailable)
async def _content_unavailable(_: Request, exc: skill_content.ContentUnavailable) -> JSONResponse:
    """读写技能仓失败。

    以前写入路径在这种情况下会**先把数据库写了**再返回 pushed=False,于是市场里看得到而远端 404。
    现在明确失败:拿"悄悄不一致"换"明确失败"。
    """
    logger.warning("skill content store unavailable: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "暂时无法读写技能仓，本次操作未保存，请稍后重试。"},
    )


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
    "/api/rsw/inventory/upload",    # external inventory tool — rsw_inventory_api_key
    # Gerrit radio/ai-assets sync — gerrit_sync_token. Called by a scheduled job,
    # not a signed-in person: making it carry a JWT would mean issuing a session
    # that can do everything else too.
    "/api/integrations/gerrit/ai-assets/sync",
    # RVC 用量推送 — ivc_push_token。同上：调用方是对方机房的无人值守服务，没有会话。
    # 不进这个白名单的话，站点级鉴权会在路由之前答 401，症状是「端点好像不存在」。
    "/api/integrations/ivc/usage/events",
    # IVC web 型资产登记 — ivc_asset_token。同上：机器入口，没有会话。
    "/api/integrations/ivc/web-assets",
}


async def _discard_body(request: Request) -> None:
    """把请求体读掉,再回错误码。

    直接回 401 会让用户看到 502。链路是这样的:本服务一响应就关连接,而前端 nginx
    还在往这边推 body —— 它的 sendfile() 拿到 EPIPE,于是没法把这个 401 转达给
    浏览器,只能自己报 502:

        [error] sendfile() failed (32: Broken pipe) while sending request to
        upstream, upstream: "http://.../api/skills/upload"

    症状因此是"上传大技能包时会话过期,界面弹 502",而不是"请重新登录" —— 前端拿不到
    401,就没法给出有用的提示。实测 5 MB 触发、1 MB 不触发:小 body 能在 401 到达之前
    发完,所以这是个竞态而非必现,别按"复现不了就没问题"处理。

    用 stream() 逐块丢弃而不是 ``await request.body()``:后者会把整个 body 收进内存,
    而走到这条路径上的调用方恰恰是**没通过鉴权**的。读多少由反代的
    client_max_body_size 决定(前端 nginx 是 50m),这里不会比它读得更多。

    没有 body 就不读:GET 占绝大多数,不该为它们白走一次 receive。
    """
    if not (
        request.headers.get("content-length")
        or request.headers.get("transfer-encoding")
    ):
        return
    try:
        async for _ in request.stream():
            pass
    except Exception:  # noqa: BLE001 - 客户端先断开就不必再管
        pass


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
        await _discard_body(request)
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


def _record_api_error(method: str, path: str, code: int, eid: str | None,
                      exc: BaseException | None) -> None:
    """同步落库，供中间件在线程池里调用。自己开会话，不碰请求那条。

    请求里那条 session 走到这里很可能已经因为异常处于需要 rollback 的状态，复用它会让
    记录本身失败 —— 而这条记录恰恰只在出错时才需要写。
    """
    from app.database import SessionLocal
    from app.services import api_errors

    with SessionLocal() as db:
        api_errors.record(
            db, method=method, path=path, status=code, eid=eid, exc=exc
        )


@app.middleware("http")
async def record_api_errors(request: Request, call_next):
    """把每一次 5xx 记进 ``rp_ai_api_error``，给 Admin 的接口报错页用。

    **两种 5xx 都要记**：抛出异常的（崩溃）和代码显式返回的（如 ``HTTPException(503)``）。
    只记前者会漏掉「服务在，它依赖的东西不在」这一整类，而那类恰恰最常被用户问起。

    **放在 ``auth_gate`` 之后注册**，因此在中间件栈里比它更外层（Starlette 把后注册的包在
    外面），能看见内层抛出的异常。记完必须原样 re-raise —— 吞掉异常会把一次 500 变成一个
    没有响应体的怪状态，而且外层 ``ServerErrorMiddleware`` 的日志也就没了。

    落库走线程池：这是一个 async 中间件，而 DB 驱动是同步的，直接调会占住事件循环。
    记录失败在 ``api_errors.record`` 里已被吞掉并落日志，所以这里不需要再包一层
    ——但仍然包了 ``try``，因为连不上库时 ``SessionLocal()`` 本身就会抛，而那不该让
    用户的响应也一起变成另一个错误。
    """
    eid = None
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001 - 记完就原样抛
        eid = str(getattr(request.state, "eid", "") or "") or None
        try:
            await run_in_threadpool(
                _record_api_error, request.method, request.url.path, 500, eid, exc
            )
        except Exception:  # noqa: BLE001
            logger.exception("记录接口报错时自己也失败了")
        raise
    if response.status_code >= 500:
        eid = str(getattr(request.state, "eid", "") or "") or None
        try:
            await run_in_threadpool(
                _record_api_error, request.method, request.url.path,
                response.status_code, eid, None,
            )
        except Exception:  # noqa: BLE001
            logger.exception("记录接口报错时自己也失败了")
    return response


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Turn DB constraint violations into a clean 400 rather than a 500.

    Log it as well. Handing the constraint text to the browser and writing
    nothing server-side means nobody can tell afterwards which constraint
    fired: the upload that hit this returned "Database constraint violation"
    with an empty log, and the cause (a duplicate eid insert) had to be
    reproduced by hand against the production schema to be found at all.
    """
    logger.error(
        "DB constraint violation on %s %s: %s",
        request.method, request.url.path, exc.orig,
    )
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
app.include_router(gerrit_sync.router)
app.include_router(ivc_sync.router)
app.include_router(ivc_web_assets.router)
app.include_router(external_usage.router)
app.include_router(reviews.router)
app.include_router(comments.router)
app.include_router(categories.router)
app.include_router(departments.router)
app.include_router(market.router)
app.include_router(agent_upload.router)
app.include_router(usage.router)
app.include_router(usage_export.router)
app.include_router(usage_report.router)
app.include_router(rsw_inventory.router)
app.include_router(rdg.router)
app.include_router(admin_assets.router)
app.include_router(api_errors_router.router)


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
