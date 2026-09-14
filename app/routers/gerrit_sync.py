"""Gerrit 资产同步端点。

**两个入口,同一段同步逻辑,两种鉴权** —— 因为调用方是两种完全不同的东西:

* ``POST /ai-assets/sync`` 给定时任务/CI:静态 bearer token,并且在 ``AUTH_WHITELIST`` 里。
  形状照 ``skill_import.py``:常量时间比较、token 没配就 503 而不是放行。
* ``POST /ai-assets/sync-now`` 给 Admin 页面上那个按钮:走站点级 JWT + ``dashboard_eids``。

为什么不让前端调第一个:浏览器手里是 JWT,不是那个静态 token。要让页面能调,就得把 token
发到浏览器 —— 那等于把一个能绕过站点鉴权的密钥给每个访客。所以第二个入口存在,而它不需要
知道那个 token。
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.repositories import gerrit_content
from app.services.eids import require_rsw_page_eid
from app.services.gerrit_assets import sync_ai_assets

router = APIRouter(prefix="/api/integrations/gerrit", tags=["gerrit-sync"])


def _run(db: Session) -> dict:
    """跑同步,把上游的两种失败翻成两个不同的状态码。

    抽出来是因为两个入口必须给出**一样**的结果和一样的错误语义 —— 一个从页面上看是 502、
    从定时任务看是 500 的系统,排查时会先怀疑错的那一层。
    """
    try:
        return sync_ai_assets(db)
    except gerrit_content.NotConfigured as exc:
        # 与"读不到"分开:这个要去配凭据,重试永远不会好。
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except gerrit_content.ContentUnavailable as exc:
        # 上游不可用是 502 而不是 500:门户没坏,它依赖的那个系统这会儿答不上来。
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/ai-assets/sync")
def sync(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    """给定时任务:扫一遍 ``radio/ai-assets`` 的 skill/ agent/ tool/ 并 upsert。

    返回体带每个资产的逐条结果,包括 owner 是**怎么**解析出来的、哪些走了兜底、以及门户里
    有但这次没见到的。同步是无人值守的,一份只有计数的返回体在归属出错时回答不了任何问题。
    """
    settings = get_settings()
    token = settings.gerrit_sync_token
    if not token:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "同步端点未启用(GERRIT_SYNC_TOKEN 没有配置)",
        )
    expected = f"Bearer {token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token 缺失或不正确")

    return _run(db)


@router.post("/ai-assets/sync-now")
def sync_now(request: Request, db: Session = Depends(get_db)) -> dict:
    """给 Admin 页面上那个按钮。和上面同一段逻辑、同一份返回体。

    **不看 ``GERRIT_SYNC_TOKEN``**:那个 token 管的是"没有会话的调用方凭什么进来",而这里
    调用方已经登录并且在 RSW 页那张名单里。让页面也依赖那个 token 会带来一个很难查的
    故障:token 忘配时页面报 503「未启用」,而人明明有权限。

    闸门是 ``require_rsw_page_eid`` 而不是 ``require_dashboard_eid``:这一页只管 RSW
    自己的资产，不该要求调用者同时有看逐人使用报告的权限（见 ``Settings.rsw_extra_page_eids``）。
    """
    require_rsw_page_eid(request, "同步 Gerrit 资产")
    return _run(db)
