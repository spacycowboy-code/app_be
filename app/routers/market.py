"""Agent市场 endpoints: list, manual create, GitLab sync, download, review, delete."""
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.market import RpAiMarketItem
from app.models.category import RpAiCategory
from app.models.market_favorite import RpAiMarketFavorite
from app.models.market_download import RpAiMarketDownload
from app.models.market_version import RpAiMarketItemVersion
from app.models.user import RpAiPortal
from app.schemas.market import (
    MarketItemCreate,
    MarketItemRead,
    MarketItemVersionRead,
    MarketReviewDecision,
    MarketReviewRead,
    MarketSyncResult,
)
from app.services import gitlab_projects as glw
from app.services.archives import rewrap_single_top_dir
from app.services.auth_tokens import create_scoped_token
from app.services.card_group import groups_for_eids
from app.services.dept_groups import PSEUDO_CATEGORY_IDS, group_for_eid
from app.services.eids import acting_eid, normalize_eid
from app.services.usage import department_of
from app.services.gitlab_sync import (
    GitLabError,
    GitLabProject,
    fetch_readme_description,
    fetch_head_commit_sha,
    fetch_repo_archive,
    list_group_projects,
)

router = APIRouter(prefix="/api/market", tags=["market"])
settings = get_settings()


def _slugify(text: str) -> str:
    import re
    import uuid

    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    # Non-ASCII names (e.g. Chinese) collapse to empty/degenerate slugs;
    # fall back to a short unique id so items don't collide.
    if len(slug) < 2:
        slug = f"item-{uuid.uuid4().hex[:8]}"
    return slug


def _norm_repo(url: str | None) -> str:
    """Canonical form of a repo URL for duplicate comparison."""
    if not url:
        return ""
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u.lower()


@router.get("", response_model=list[MarketItemRead])
def list_items(
    request: Request,
    db: Session = Depends(get_db),
    category_id: str | None = Query(None, alias="categoryId"),
    item_type: str | None = Query(None, alias="itemType"),
    service_type: str | None = Query(None, alias="serviceType"),
    q: str | None = Query(None),
    x_user_eid: str | None = Header(None),
) -> list[RpAiMarketItem]:
    """列出市场条目,只包含请求者看得到的那些。

    ``pending`` 的条目除上传者和 admin 谁都看不到。包上传现在传完即上市,所以新条目不会是
    pending —— 这条规则留着是因为**下架靠的就是它**:管理员在审核页拒绝,条目立刻从所有人
    的列表里消失,而上传者仍然看得到自己的那一条(否则他不知道发生了什么)。

    过滤必须落在数据层而不是靠界面不显示:条目 id 是可推出来的(``gl-<GitLab 项目 id>``),
    "列表里看不见"和"接口取不到"是两件事,少一件等于没有。
    """
    stmt = select(RpAiMarketItem)
    requester = acting_eid(request, x_user_eid)
    if requester != settings.audit_admin_eid:
        stmt = stmt.where(
            (RpAiMarketItem.status == "approved") | (RpAiMarketItem.creator == requester)
        )
    if category_id and category_id != "all":
        stmt = stmt.where(RpAiMarketItem.category_id == category_id)
    if item_type:
        stmt = stmt.where(RpAiMarketItem.item_type == item_type)
    if service_type:
        stmt = stmt.where(RpAiMarketItem.service_type == service_type)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            RpAiMarketItem.name.like(like) | RpAiMarketItem.description.like(like)
        )
    items = list(db.scalars(stmt.order_by(RpAiMarketItem.name)).all())
    _attach_stats(db, items)
    return items


def _require_installable(item: RpAiMarketItem, requester: str | None) -> None:
    """待审核的包只有上传者与 admin 能取。

    这道闸门在两个地方都要有(打包下载、发安装令牌),因为它们是两条独立的取件路径。列表里
    看不见不算闸门:条目 id 是可猜的(``gl-<项目id>``),而"看不见"和"取不到"是两件事。
    """
    if item.status == "approved":
        return
    if requester and (requester == item.creator or requester == settings.audit_admin_eid):
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "这个条目还在等待审核,通过之后才能安装。",
    )


def _attach_stats(db: Session, items: list[RpAiMarketItem]) -> None:
    """Attach deduplicated-by-EID favorite/download counts onto each item,
    plus the creator's org group.

    Favorites are already unique per (eid, item) by table constraint, so the
    row count is the like count. Downloads are raw events, so we count distinct
    EIDs (anonymous/NULL EIDs are excluded from the unique-user count).

    The group rides along here rather than in ``list_items`` because every path
    that returns a ``MarketItemRead`` goes through this function: the list, the
    single item, and the review page. A card that shows the group on the market
    and loses it on the detail view reads as a bug in the data.
    """
    if not items:
        return
    ids = [it.id for it in items]
    fav = dict(
        db.execute(
            select(RpAiMarketFavorite.item_id, func.count(RpAiMarketFavorite.id))
            .where(RpAiMarketFavorite.item_id.in_(ids))
            .group_by(RpAiMarketFavorite.item_id)
        ).all()
    )
    dls = dict(
        db.execute(
            select(
                RpAiMarketDownload.item_id,
                func.count(func.distinct(RpAiMarketDownload.eid)),
            )
            .where(RpAiMarketDownload.item_id.in_(ids))
            .group_by(RpAiMarketDownload.item_id)
        ).all()
    )
    # One query for the whole page. GitLab-synced items have no creator, so they
    # contribute no EID and end up with no group — correctly: there is no person
    # to place, and inventing one would credit an office that never uploaded it.
    groups = groups_for_eids(db, {it.creator for it in items if it.creator})
    for it in items:
        it.favorite_count = int(fav.get(it.id, 0))
        it.download_count = int(dls.get(it.id, 0))
        it.group = groups.get((it.creator or "").strip().upper())


@router.post("/{item_id}/download")
def record_download(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    eid: str | None = Query(default=None),
):
    """Serve a market item's repository as a .zip **and** record the download.

    **Why the portal serves the archive instead of pointing at GitLab.** Most
    colleagues have no access to the internal GitLab and do not want to request
    it, so "clone this repo" excludes almost everyone this market exists for.
    The portal already holds a read token (used by the subgroup sync), so it can
    hand over the code while the user only needs a portal login.

    The project id comes from our own row, never from the caller: synced items
    are keyed ``gl-<gitlab project id>``, which is exactly what the archive API
    wants. Letting a caller name the project would turn this endpoint into a way
    to read arbitrary internal repos through the portal's credentials.

    Only **synced** items can be served: a manual item has no GitLab project
    behind it, just a link someone typed. Serving those would mean trusting a
    user-supplied URL, which is the same hole in a different shape.

    The download event is recorded **before** the transfer, and a failed fetch
    raises. Recording after a successful transfer would look tidier but loses
    every attempt that failed — and "nobody can download it" is precisely the
    thing the counter should make visible.
    """
    item = db.get(RpAiMarketItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")

    # Identity comes from the JWT (set by the auth middleware), falling back to
    # the query param only when auth is off in dev. The middleware's whole point
    # is that handlers stop trusting a client-supplied eid.
    downloader = acting_eid(request, eid)
    _require_installable(item, downloader)
    db.add(
        RpAiMarketDownload(
            item_id=item_id,
            eid=downloader,
            department=department_of(db, downloader),
        )
    )
    db.commit()

    if not item.id.startswith("gl-"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "该项目不是从 GitLab 同步来的，没有可下载的代码包；请改用它的仓库地址",
        )
    project_id = item.id[3:]
    try:
        blob = fetch_repo_archive(project_id)
    except GitLabError as exc:
        # 502, not 500: the portal is fine, the upstream it depends on is not.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"从 GitLab 取代码包失败：{exc}"
        ) from exc

    filename = f"{_slugify(item.name) or item.id}.zip"
    # Re-pack so extracting gives exactly one folder, named the same as the zip.
    #
    # GitLab wraps its archive in ``<project>-<ref>-<sha>/``, so a raw pass-through
    # extracts to e.g. ``report-manhour-main-3f8a9c2e/`` — a level deeper than
    # people expect, under a name that changes every commit. Every instruction
    # that says ``cd report-manhour`` would be wrong, and wrong-by-default
    # instructions are worse than missing ones.
    blob = rewrap_single_top_dir(blob, filename[:-4])
    return StreamingResponse(
        io.BytesIO(blob),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(blob)),
        },
    )


@router.post("/{item_id}/install-token")
def create_install_token(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> dict:
    """Mint a short-lived token that can fetch **this one** archive and nothing else.

    Why this exists: installing one of these tools means an agent on the user's
    machine has to fetch the archive, and that agent holds no portal session.
    The alternatives were worse — dropping auth on the download endpoint makes
    every synced repository world-readable to anyone who can reach the portal,
    and having the agent collect the user's domain password both trains people to
    type corporate credentials into agent prompts and sends them over a portal
    that has no TLS yet.

    So the human authenticates in the browser (where they already are) and the
    portal hands out a credential narrow enough to paste into a terminal: one
    path, one method, a few minutes.

    Requires a normal session — this route is behind the site-wide gate, and a
    scoped token cannot reach it (its own scope is the download path), so these
    tokens cannot be used to mint more of themselves.
    """
    item = db.get(RpAiMarketItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    if not item.id.startswith("gl-"):
        # Same rule as the download endpoint: nothing to fetch for manual items.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "该项目不是从 GitLab 同步来的，没有可下载的代码包；请改用它的仓库地址",
        )

    # 身份走 acting_eid:签名的 JWT 优先,客户端声明只在 auth_enabled=false 时兜底。此前这里
    # 只读 request.state,于是在那个模式下**谁都没有身份**,连上传者也取不到自己那份待审的包。
    eid = acting_eid(request, x_user_eid)
    _require_installable(item, eid)
    project_id = item.id[3:]

    # The path is built here, from the item we just loaded — it is never taken
    # from the caller. Accepting a caller-supplied path would turn this into
    # "sign me a token for any endpoint I name", which is strictly worse than
    # the session the caller already has, because a scoped token is meant to
    # leave the browser and live in shell history and agent transcripts.
    path = f"{router.prefix}/{item.id}/download"
    token, expires_in = create_scoped_token(eid or "", path, method="POST")

    # 上传的包有确认过的启动信息,同步来的没有。
    #
    # 这一段是安装提示词的来源。此前提示词让对方的 agent 去 README 里"原样抄"启动命令,并写
    # 明"找不到就问我,不要猜" —— 因为同步来的仓库我们确实不知道怎么启动它。上传这条路上
    # 我们是知道的:上传者在确认屏里核对过。把它原文带上,猜错这件事就不存在了。
    #
    # `runtime` 同理:提示词原来无条件写"跑起来会弹出浏览器窗口、不要关掉",那对一次性脚本
    # 是错的,而错在这种地方的提示会让人怀疑整段说明。
    latest = db.scalars(
        select(RpAiMarketItemVersion)
        .where(RpAiMarketItemVersion.item_id == item.id)
        .order_by(RpAiMarketItemVersion.id.desc())
        .limit(1)
    ).first()
    meta = (latest.metadata_json or {}) if latest else {}
    return {
        "token": token,
        "expiresIn": expires_in,
        "path": path,
        "filename": f"{_slugify(item.name) or item.id}.zip",
        # Lets a re-install skip the whole unpack when the code has not changed.
        # ``None`` when GitLab could not be asked — the caller then reinstalls,
        # which is the pre-existing behaviour, so this never blocks anything.
        # 上传的包用语义版本(1.1.0),那也是写进 .installed-version 的值 —— 一个人看得懂的
        # 版本号,而不是一串 sha。
        "version": (latest.version if latest else None) or fetch_head_commit_sha(project_id),
        "launch": meta.get("launch") or None,
        "runtime": meta.get("runtime") or None,
        "keep": meta.get("keep") or [],
        # 提示词第 4 步原来只有一句泛泛的"按 README 安装(若有 setup.ps1 就跑它)",把判断
        # 推给了对方的 agent。这两项让它不必判断:install 是门户从包里推出来的精确命令,
        # prerequisites 是上传者点名的、开始装之前必须知道的事(独占资源、要先登出的会话)。
        # 都可能是空的 —— 空就退回原来那句,那是诚实的兜底。
        "install": meta.get("install") or [],
        "prerequisites": meta.get("prerequisites") or [],
    }


@router.post("", response_model=MarketItemRead, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: MarketItemCreate,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> RpAiMarketItem:
    """Manual upload: Code (repo url) or Cloud (service url), Tool or Agent.

    Identity via ``acting_eid``: the signed token decides and the header is only
    the fallback for ``auth_enabled=false``. Reading the header directly (what
    this did) meant any signed-in user could send ``X-User-Eid: ADMINMM`` and
    take the admin branch — the gate validated a token and then authorised on a
    value the caller typed.
    """
    creator = acting_eid(request, x_user_eid)
    if payload.item_type == "ai-platform" and creator != settings.audit_admin_eid:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "只有管理员可以创建 AI Platform 类型"
        )
    if payload.service_type == "code" and not payload.repo_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Code 服务需要填写仓库地址")
    if payload.service_type == "cloud" and not payload.service_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "云服务需要填写服务地址")

    # For Code services, reject a repo URL that already exists in the market
    # (including items synced from the two GitLab subgroups).
    if payload.service_type == "code":
        target = _norm_repo(payload.repo_url)
        existing = db.scalars(
            select(RpAiMarketItem).where(RpAiMarketItem.repo_url.isnot(None))
        ).all()
        dup = next((it for it in existing if _norm_repo(it.repo_url) == target), None)
        if dup is not None:
            where = "GitLab 同步库" if dup.source.startswith("gitlab") else "市场"
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"该仓库地址已存在于{where}：{dup.name}",
            )

    data = payload.model_dump()

    # Category: same shape as the skill upload path (routers/upload.py) on
    # purpose — one behaviour for "which group does this belong to", not two.
    #
    # This path used to write ``payload.category_id`` straight through with no
    # check at all, and the registration dialog defaulted it to 'public'. Because
    # 'public' is a real, selectable category, nothing looked wrong and nothing
    # could complain: three IVC agents registered on 2026-09-01 landed in Public
    # and were simply not there when their author filtered by IVC.
    #
    # Validating here and not only in the dialog: the endpoint is public and the
    # item id is guessable, so "the UI now sends a group" is not the same as "a
    # row cannot be filed wrong". UI reachability is not a boundary.
    category_id = (data.get("category_id") or "").strip()
    if not category_id:
        category_id = group_for_eid(db, creator) or ""
    if not category_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "请选择分组:你的部门没有对应的门户分组。",
        )
    if category_id in PSEUDO_CATEGORY_IDS:
        # 'all' is a filter, not a home; 'official' claims we vetted it.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"'{category_id}' 不是可以登记到的分组。"
        )
    if db.get(RpAiCategory, category_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知分组 '{category_id}'。")
    data["category_id"] = category_id

    base = _slugify(payload.name)
    item_id = base
    n = 2
    while db.get(RpAiMarketItem, item_id):
        item_id = f"{base}-{n}"
        n += 1

    item = RpAiMarketItem(id=item_id, source="manual", creator=creator, **data)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> None:
    item = db.get(RpAiMarketItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    # Same reason as create_item: authorise on the verified identity, not on a
    # header the caller controls.
    requester = acting_eid(request, x_user_eid)
    is_admin = requester == settings.audit_admin_eid
    # `item.creator` 为空时**不放行**。原来的写法是 `item.creator and requester != ...`,
    # 于是没有主人的条目谁都能动 —— 而同步进来的条目 creator 就是 NULL,它们的 id 又是可以
    # 推出来的 `gl-<GitLab 项目 id>`。查不到主人时该做的是交给管理员,不是对所有人开放。
    if not is_admin and requester != item.creator:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "只有上传者本人或管理员可以删除这个条目"
            if item.creator
            else "这个条目没有登记上传者,只有管理员可以删除",
        )
    db.delete(item)
    db.commit()


@router.put("/{item_id}", response_model=MarketItemRead)
def update_item(
    item_id: str,
    payload: MarketItemCreate,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> RpAiMarketItem:
    """Edit a market item. Only its creator, or an admin.

    原来这里写的是"creator 本人,**或者没有 creator 的条目**",而那不是一条规则、是一个洞:
    `MarketItemCreate` 里带 `repo_url` / `service_url`,所以"任何人可以编辑没有主人的条目"
    等于任何人可以把一个同步进来的条目改指向自己的仓库,而卡片、名字、下载入口一个字不变。
    没有主人的条目现在交给管理员。
    """
    item = db.get(RpAiMarketItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")

    requester = acting_eid(request, x_user_eid)
    is_admin = requester == settings.audit_admin_eid
    # `item.creator` 为空时**不放行**。原来的写法是 `item.creator and requester != ...`,
    # 于是没有主人的条目谁都能动 —— 而同步进来的条目 creator 就是 NULL,它们的 id 又是可以
    # 推出来的 `gl-<GitLab 项目 id>`。查不到主人时该做的是交给管理员,不是对所有人开放。
    if not is_admin and requester != item.creator:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "只有创建者本人可以编辑"
            if item.creator
            else "这个条目没有登记创建者,只有管理员可以编辑",
        )

    if payload.item_type == "ai-platform" and not is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "只有管理员可以设置 AI Platform 类型"
        )

    # Update mutable fields.
    item.name = payload.name
    item.description = payload.description
    item.item_type = payload.item_type
    item.service_type = payload.service_type
    item.repo_url = payload.repo_url
    item.service_url = payload.service_url
    item.embeddable = payload.embeddable
    item.category_id = payload.category_id
    item.category = payload.category
    item.dept_color = payload.dept_color
    item.tags = payload.tags or []
    item.author = payload.author
    item.version = payload.version

    db.commit()
    db.refresh(item)
    return item


def _upsert_project(
    db: Session, proj: GitLabProject, item_type: str, source: str
) -> bool:
    """Upsert a market item from a GitLab project. Returns True if created."""
    item_id = f"gl-{proj.id}"
    item = db.get(RpAiMarketItem, item_id)
    created = item is None
    if item is None:
        item = RpAiMarketItem(id=item_id)
        db.add(item)
    item.name = proj.name
    # Description = the README section whose heading matches the repo name;
    # if there is none, show nothing (per requirement).
    item.description = fetch_readme_description(proj.id, proj.default_branch, proj.name)
    item.item_type = item_type
    item.service_type = "code"
    item.source = source
    item.repo_url = proj.web_url
    item.tags = proj.topics or []
    item.category = proj.namespace_name
    item.last_activity = proj.last_activity_at
    item.verified = True
    if not item.version:
        item.version = "1.0"
    return created


@router.get("/{item_id}/versions", response_model=list[MarketItemVersionRead])
def list_item_versions(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> list[RpAiMarketItemVersion]:
    """Version history of an uploaded agent package, newest first."""
    item = db.get(RpAiMarketItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    requester = acting_eid(request, x_user_eid)
    # Same rule as the list: a package awaiting review is nobody else's business.
    if item.status != "approved" and requester not in (
        item.creator, settings.audit_admin_eid
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "这个条目还在等待审核。")
    rows = list(
        db.scalars(
            select(RpAiMarketItemVersion)
            .where(RpAiMarketItemVersion.item_id == item_id)
            .order_by(RpAiMarketItemVersion.id.desc())
        ).all()
    )
    return rows


@router.get("/{item_id}/review", response_model=MarketReviewRead)
def get_review(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> MarketReviewRead:
    """Assemble what a reviewer needs to decide.

    The audit does not block anything, so the person reading this page is the
    only gate. That is why the file list comes from GitLab live and the compare
    link is real: an approval based on a summary someone else wrote is a rubber
    stamp. What is *not* real yet is the scored review — there is no reviewing
    backend, and the page says so rather than showing an empty verdict.
    """
    item = db.get(RpAiMarketItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    requester = acting_eid(request, x_user_eid)
    if item.status != "approved" and requester not in (
        item.creator, settings.audit_admin_eid
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "这个条目还在等待审核。")

    versions = list(
        db.scalars(
            select(RpAiMarketItemVersion)
            .where(RpAiMarketItemVersion.item_id == item_id)
            .order_by(RpAiMarketItemVersion.id.desc())
        ).all()
    )
    files: list[dict] = []
    files_error: str | None = None
    compare: str | None = None
    if item.id.startswith("gl-") and versions:
        ref = versions[0].tag or "HEAD"
        if item.source != "upload":
            # 同步来的条目住在 sync 的那两个组里,而这里的读用的是**上传**组的组访问令牌 ——
            # 问不到,GitLab 答 404。说清楚原因,而不是显示一个空清单:"这个包没有文件"和
            # "我们问不到" 对一个要决定是否发布的人不是同一件事。
            files_error = "这个条目不是通过门户上传的,文件清单请到它的 GitLab 项目里看。"
        else:
            try:
                # 用哪个 token 取决于这个条目是什么:两个上传组各有自己的组访问令牌,拿错
                # 那个问不到这个项目(GitLab 答 404,而组名是对的)。
                files = [
                    {"path": e.get("path"), "name": e.get("name")}
                    for e in glw.list_tree(
                        item.id[3:], ref, target=glw.target_for(item.item_type)
                    )
                ]
            except glw.GitLabWriteError as exc:
                # Say why instead of showing an empty list: "this package has no
                # files" and "we could not ask GitLab" must not look the same to
                # someone deciding whether to publish it.
                files_error = exc.detail
        if len(versions) > 1 and versions[1].tag and versions[0].tag and item.repo_url:
            compare = glw.compare_url(item.repo_url, versions[1].tag, versions[0].tag)

    # 同样按 eid 查,不能用 db.get():主键是自增的 id,eid 只是唯一索引。db.get 在这里不会
    # 报错,只会永远答 None —— 审核页上传者那一栏就永远只有 EID、没有姓名和部门。
    uploader = (
        db.scalar(select(RpAiPortal).where(RpAiPortal.eid == item.creator))
        if item.creator
        else None
    )
    _attach_stats(db, [item])
    return MarketReviewRead(
        item=MarketItemRead.model_validate(item, from_attributes=True),
        versions=[
            MarketItemVersionRead.model_validate(v, from_attributes=True) for v in versions
        ],
        files=files,
        files_error=files_error,
        compare_url=compare,
        project_url=item.repo_url,
        uploader_name=(uploader.display_name if uploader else None) or item.creator,
    )


@router.post("/{item_id}/review", response_model=MarketItemRead)
def decide_review(
    item_id: str,
    payload: MarketReviewDecision,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> RpAiMarketItem:
    """Take an uploaded package off the market, or put it back. Admin only, and recorded.

    This is no longer a gate on the way in — packages are approved on upload, so
    nothing waits here. What it is now is the retract switch: `rejected` (or
    `pending`) hides the item from everyone except its uploader, immediately,
    because `list_items` filters in the data layer.

    Recording *who* and *when* still matters, and more than before: with no entry
    gate, "who took this down" (or put it back) is the only trace of a judgement
    call anyone made about this item.
    """
    requester = acting_eid(request, x_user_eid)
    if requester != settings.audit_admin_eid:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有管理员可以审核")
    item = db.get(RpAiMarketItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    item.status = payload.status
    item.reviewed_by = requester
    item.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    _attach_stats(db, [item])
    return item


@router.post("/sync", response_model=MarketSyncResult)
def sync_from_gitlab(db: Session = Depends(get_db)) -> MarketSyncResult:
    """Sync items from the configured GitLab subgroups.

    Projects in the agents subgroup become Agents; projects in the tools
    subgroup become Tools.
    """
    plan = [
        (settings.gitlab_agents_group, "agent", "gitlab-agents"),
        (settings.gitlab_tools_group, "tool", "gitlab-tools"),
    ]
    created = updated = 0
    groups: list[str] = []
    try:
        for group_path, item_type, source in plan:
            projects = list_group_projects(group_path)
            groups.append(f"{group_path} ({len(projects)})")
            for proj in projects:
                if _upsert_project(db, proj, item_type, source):
                    created += 1
                else:
                    updated += 1
        db.commit()
    except GitLabError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return MarketSyncResult(
        synced=created + updated,
        created=created,
        updated=updated,
        groups=groups,
        message=f"已同步 {created + updated} 个项目（新增 {created}，更新 {updated}）",
    )
