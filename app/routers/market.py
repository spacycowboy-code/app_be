"""Agent市场 endpoints: list, manual create, GitLab sync, download, review, delete."""
import io
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.market import RpAiMarketItem
from app.models.category import RpAiCategory
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
from app.services import rdg_tags
from app.services.archives import rewrap_single_top_dir
from app.services.asset_stats import attach_market_stats
from app.services.auth_tokens import create_scoped_token
from app.services.dept_groups import (
    FALLBACK_GROUP_ID,
    PSEUDO_CATEGORY_IDS,
    group_for_eid,
)
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

log = logging.getLogger(__name__)

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
    attach_market_stats(db, items)
    return items


@router.get("/{item_id}", response_model=MarketItemRead)
def get_item(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> RpAiMarketItem:
    """取单个市场条目 —— 详情页与分享链接用的就是这个。

    **可见性规则必须和 ``list_items`` 一字不差**,这不是整齐好看的问题。下架的实现方式就是
    这条规则:管理员在审核页拒绝,条目的 status 变成非 approved,于是它从所有人的列表里消失。
    而条目 id 是可推出来的(``gl-<GitLab 项目 id>``)。所以这个按 id 取的接口若少了这道闸,
    等于给任何拿到过链接的人留了一条把已下架条目重新捞出来的路 —— 而且没有任何报错,列表页
    看起来一切正常。``list_items`` 的 docstring 已经把这件事写过一次:"列表里看不见"和
    "接口取不到"是两件事,少一件等于没有。

    找不到与看不到都答 404,不答 403:403 会告诉一个没权限的人"这个 id 确实存在",而 id 是
    可推的,那就等于把下架条目的清单也一起交出去了。
    """
    item = db.get(RpAiMarketItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到这个条目。")

    requester = acting_eid(request, x_user_eid)
    if requester != settings.audit_admin_eid and item.status != "approved":
        if item.creator != requester:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到这个条目。")

    # 详情页要显示下载次数,和列表页同一个来源。
    attach_market_stats(db, [item])
    return item


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
    user-supplied URL, which is the same hole in a different shape. Items whose
    package lives upstream (``external_url``, set by the IVC/RVC index sync) are
    the one exception, and they are answered with a 307 rather than proxied — the
    portal never fetches those bytes either.

    The download event is recorded **before the transfer** but **after the static
    preconditions**. A failed fetch still counts, deliberately: "nobody can
    download it" is precisely the thing the counter should make visible. An item
    that has no package *at all* does not count, because that failure never
    resolves — counting it inflates the number permanently and invisibly.
    """
    item = db.get(RpAiMarketItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")

    # Identity comes from the JWT (set by the auth middleware), falling back to
    # the query param only when auth is off in dev. The middleware's whole point
    # is that handlers stop trusting a client-supplied eid.
    downloader = acting_eid(request, eid)
    _require_installable(item, downloader)

    # **静态前置条件挡在记流水之前，瞬时失败挡在之后。** 两者性质不同，不能共用一侧：
    #
    # * 「这个条目根本没有包」是永久事实 —— 一个只有链接的登记条目永远不会变成可下载。把它记成
    #   一次下载，计数就永久偏高，而且看不出任何异常：那一行说「有人下载过」，而事实是那个人拿到
    #   了一个报错弹窗。
    # * 「GitLab 这会儿取不到包」是瞬时失败，记在前面是**有意**的（见下面 502 那段）——
    #   「大家都下不下来」这件事必须能从计数里看出来。
    #
    # 条件里必须带上 ``externally_hosted``：上游托管的条目 id 不是 ``gl-`` 开头，但它有地址、
    # 下载是通的。漏掉这一项会把刚接进来的 agent 全部挡死，而症状是「下载按钮点了没反应」。
    if not item.externally_hosted and not item.id.startswith("gl-"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "该项目不是从 GitLab 同步来的，没有可下载的代码包；请改用它的仓库地址",
        )

    db.add(
        RpAiMarketDownload(
            item_id=item_id,
            eid=downloader,
            department=department_of(db, downloader),
        )
    )
    db.commit()

    # 上游托管的包：跳过去，不在这边取字节。放在记流水之后是对的 —— 这是一条**成功**路径，
    # 跳转本身就是下载。
    #
    # 307 而不是 302：302 允许客户端把方法改成 GET，而这是一个 POST；被改写后 Nexus 那边看到的
    # 是一个它不认的请求，症状是浏览器里一个说不清的失败而门户日志干净。技能侧
    # （``skills.py`` 的下载路由）已经是 307，这里保持一致。
    #
    # 注意前端必须用 ``<a href>`` 而不是 XHR 去打这个端点：XHR 跟不了到 Nexus 的跨域 307，
    # 只显示 Network error，而服务端日志完全正常。2026-09-01 在技能侧踩过一次。
    if item.externally_hosted:
        return RedirectResponse(item.external_url, status_code=307)
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
    # ``rdg_nodes`` 不是这张表上的列(它落在 ``rp_ai_rdg_asset_tag``),所以要先摘出来 ——
    # 留在 dict 里会让下面的 ``RpAiMarketItem(**data)`` 直接 TypeError。
    wanted_rdg_nodes = data.pop("rdg_nodes", None) or []

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
    # R&D Guide 关联和条目一起落。校验用**已经解析并校验过的** ``category_id``(上面那一段
    # 会在为空时按部门兜底),不是 ``payload.category_id`` —— 后者可能是空的,那样一个 RSW 的人
    # 登记条目挂关联会被拒,而他挂的是对的。
    if wanted_rdg_nodes:
        rejection = rdg_tags.rejection_for(db, wanted_rdg_nodes, category_id)
        if rejection:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, rejection)
        rdg_tags.set_tags(db, "item", item_id, wanted_rdg_nodes)
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
    # 关联表刻意没有外键(见 ``models/rdg.py``),所以级联删除得显式做。不清的话 R&D Guide
    # 卡片上那个数字会把已经不存在的条目算进去 —— 点进去是空的计数比没有计数更坏。
    rdg_tags.forget(db, "item", item_id)
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
    # 漏掉这一行的表现是"编辑弹窗里填了 spec 地址，保存后又不见了" —— 创建走
    # ``model_dump()`` 全量透传，编辑是逐字段赋值，所以新增列必须在这里补一笔。
    item.openapi_url = payload.openapi_url
    item.embeddable = payload.embeddable
    item.category_id = payload.category_id
    item.category = payload.category
    item.dept_color = payload.dept_color
    item.tags = payload.tags or []
    item.author = payload.author
    item.version = payload.version

    # R&D Guide 关联。``None`` 是「这次不改」,``[]`` 是「清空」—— 见 ``MarketItemCreate``
    # 那个字段的注释。区分这两者是必须的:编辑弹窗如果不带这个字段(比如它压根不显示这一格),
    # 按「清空」处理就会在用户改个描述的时候静默把关联删掉。
    #
    # 校验用**这次要落的分类**而不是条目原来的分类:这个接口能改 ``category_id``,所以
    # 「把 RSW 的资产改成别的部门」这一步要连带否掉它身上的关联,否则那些关联会留在一个
    # 已经不属于 RSW 的资产上,而卡片仍然把它算进去。
    if payload.rdg_nodes is not None:
        rejection = rdg_tags.rejection_for(db, payload.rdg_nodes, payload.category_id)
        if rejection:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, rejection)
        rdg_tags.set_tags(db, "item", item.id, payload.rdg_nodes)
    elif payload.category_id != rdg_tags.RSW_CATEGORY_ID:
        # 分类被改出 RSW,而这次没提关联。留着会让卡片继续算一个不属于 RSW 的资产。
        rdg_tags.forget(db, "item", item.id)

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
    # 分组:GitLab 项目没有工号可查(这条路一个 ``creator`` 都不写),所以推不出部门,归 ``others``。
    #
    # 以前这里只写 ``item.category = proj.namespace_name``。那是**显示用的标签**,不是分组 id ——
    # 于是卡片上写着 "RP-AI-AGENT",而 ``category_id`` 是空的:计入 "All",不出现在任何部门标签
    # 下。两个字段一个填一个不填,看起来像有分组,筛起来没有。
    #
    # 现在两个字段一起写,并且 label 取分类行自己的,而不是 GitLab 命名空间 —— 否则卡片显示
    # "RP-AI-AGENT" 却按 "Others" 过滤,还是同一种口径不一致。
    # 分类行缺失时不硬塞:``category_id`` 是外键,给一个没有对应行的值会让 INSERT 直接
    # IntegrityError,整次同步连带其它项目一起挂。
    cat = db.get(RpAiCategory, FALLBACK_GROUP_ID)
    if cat is not None:
        item.category_id = cat.id
        item.category = cat.label
    else:
        log.warning("没有 %r 分类行,%s 先不归组", FALLBACK_GROUP_ID, item_id)
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

    uploader = (
        db.scalar(select(RpAiPortal).where(RpAiPortal.eid == item.creator))
        if item.creator
        else None
    )
    attach_market_stats(db, [item])
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
    attach_market_stats(db, [item])
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
