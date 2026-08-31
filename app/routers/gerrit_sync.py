"""Gerrit 资产同步端点。

形状照 ``skill_import.py``:静态 bearer token、常量时间比较、token 没配就 503 而不是放行,
并且在 ``AUTH_WHITELIST`` 里 —— 调用方是一个定时任务或 CI,不是一个登录用户,让它去拿 JWT
等于给它一个能干别的事的凭据。

先只做手动触发。跑顺、看清返回体里那份逐条结果之后再上定时,这样第一次同步出的任何意外
都发生在有人盯着的时候。
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.repositories import gerrit_content
from app.services.gerrit_assets import sync_ai_assets

router = APIRouter(prefix="/api/integrations/gerrit", tags=["gerrit-sync"])


@router.post("/ai-assets/sync")
def sync(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    """扫一遍 ``radio/ai-assets`` 的 skill/ agent/ tool/ 并 upsert 进门户。

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

    try:
        return sync_ai_assets(db)
    except gerrit_content.NotConfigured as exc:
        # 与"读不到"分开:这个要去配凭据,重试永远不会好。
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except gerrit_content.ContentUnavailable as exc:
        # 上游不可用是 502 而不是 500:门户没坏,它依赖的那个系统这会儿答不上来。
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
