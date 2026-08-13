"""Agent市场 endpoints: list, manual create, GitLab sync, delete."""
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.market import RpAiMarketItem
from app.models.market_favorite import RpAiMarketFavorite
from app.models.market_download import RpAiMarketDownload
from app.schemas.market import MarketItemCreate, MarketItemRead, MarketSyncResult
from app.services.eids import normalize_eid
from app.services.gitlab_sync import (
    GitLabError,
    GitLabProject,
    fetch_readme_description,
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


@router.post("/{item_id}/download", status_code=status.HTTP_204_NO_CONTENT)
def record_download(
    item_id: str,
    db: Session = Depends(get_db),
    eid: str | None = Query(default=None),
) -> None:
    """Record a download event for a market item (for unique-downloader counts).

    The archive itself is served by GitLab; the frontend calls this so the
    download is attributed to the user's EID (deduplicated at read time).
    """
    if db.get(RpAiMarketItem, item_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    db.add(RpAiMarketDownload(item_id=item_id, eid=normalize_eid(eid) or None))
    db.commit()


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
