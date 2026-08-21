"""Agent市场 endpoints: list, manual create, GitLab sync, download, delete."""
import io

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.market import RpAiMarketItem
from app.models.market_favorite import RpAiMarketFavorite
from app.models.market_download import RpAiMarketDownload
from app.schemas.market import MarketItemCreate, MarketItemRead, MarketSyncResult
from app.services.archives import rewrap_single_top_dir
from app.services.auth_tokens import create_scoped_token
from app.services.eids import normalize_eid
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
    db: Session = Depends(get_db),
    category_id: str | None = Query(None, alias="categoryId"),
    item_type: str | None = Query(None, alias="itemType"),
    service_type: str | None = Query(None, alias="serviceType"),
    q: str | None = Query(None),
) -> list[RpAiMarketItem]:
    stmt = select(RpAiMarketItem)
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


def _attach_stats(db: Session, items: list[RpAiMarketItem]) -> None:
    """Attach deduplicated-by-EID favorite/download counts onto each item.

    Favorites are already unique per (eid, item) by table constraint, so the
    row count is the like count. Downloads are raw events, so we count distinct
    EIDs (anonymous/NULL EIDs are excluded from the unique-user count).
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
    for it in items:
        it.favorite_count = int(fav.get(it.id, 0))
        it.download_count = int(dls.get(it.id, 0))


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
    caller_eid = getattr(request.state, "eid", None) or eid
    downloader = normalize_eid(caller_eid) or None
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

    eid = getattr(request.state, "eid", None)
    project_id = item.id[3:]

    # The path is built here, from the item we just loaded — it is never taken
    # from the caller. Accepting a caller-supplied path would turn this into
    # "sign me a token for any endpoint I name", which is strictly worse than
    # the session the caller already has, because a scoped token is meant to
    # leave the browser and live in shell history and agent transcripts.
    path = f"{router.prefix}/{item.id}/download"
    token, expires_in = create_scoped_token(eid or "", path, method="POST")
    return {
        "token": token,
        "expiresIn": expires_in,
        "path": path,
        "filename": f"{_slugify(item.name) or item.id}.zip",
        # Lets a re-install skip the whole unpack when the code has not changed.
        # ``None`` when GitLab could not be asked — the caller then reinstalls,
        # which is the pre-existing behaviour, so this never blocks anything.
        "version": fetch_head_commit_sha(project_id),
    }


@router.post("", response_model=MarketItemRead, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: MarketItemCreate,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> RpAiMarketItem:
    """Manual upload: Code (repo url) or Cloud (service url), Tool or Agent."""
    creator = normalize_eid(x_user_eid)
    if payload.item_type == "ai-platform" and creator != "ADMINMM":
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
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> None:
    item = db.get(RpAiMarketItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    requester = normalize_eid(x_user_eid)
    is_admin = requester == "ADMINMM"
    if not is_admin and item.creator and requester != item.creator:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admin or creator can delete")
    db.delete(item)
    db.commit()


@router.put("/{item_id}", response_model=MarketItemRead)
def update_item(
    item_id: str,
    payload: MarketItemCreate,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> RpAiMarketItem:
    """Edit a market item. Only the creator (or items without a creator) may edit."""
    item = db.get(RpAiMarketItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")

    requester = normalize_eid(x_user_eid)
    is_admin = requester == "ADMINMM"
    if not is_admin and item.creator and requester != item.creator:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有创建者本人可以编辑")

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
