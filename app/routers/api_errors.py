"""Admin 的接口报错页：``/api/admin/api-errors``。

**谁能用。** 和 Dashboard 同一份名单（``dashboard_eids``），走 ``services.eids`` 里那个
共用的 ``require_dashboard_eid``。这不是可选的谨慎：这一页会吐出调用栈与触发者工号，而
前端侧边栏的 ``visibleTo`` 只是不打扰普通用户、**不是安全边界**（那个仓的 sidebar spec
里就写着这句话）。任何登录用户都能直接打这个接口，所以闸门必须在这里。

三个端点：列表、立刻重跑诊断、标记已处理。写操作都是幂等的（重复点第二次不会坏事）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.api_error import RpAiApiError
from app.schemas.api_error import ApiErrorList, ApiErrorRead
from app.services import api_errors
from app.services.eids import require_dashboard_eid

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/api-errors", tags=["admin-api-errors"])


def _gate(request: Request) -> str:
    return require_dashboard_eid(request, "查看接口报错")


def _get_or_404(db: Session, error_id: int) -> RpAiApiError:
    row = db.get(RpAiApiError, error_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有这条报错记录")
    return row


@router.get("", response_model=ApiErrorList, summary="接口报错列表（Dashboard 名单可见）")
def list_errors(
    limit: int = Query(100, ge=1, le=500),
    include_resolved: bool = Query(False, alias="includeResolved"),
    db: Session = Depends(get_db),
    _eid: str = Depends(_gate),
) -> ApiErrorList:
    """按最近发生倒序。

    ``total`` 是**按同一个过滤条件**的总数，不是全表行数 —— 页面上写「共 N 条」时两者不同
    会让人以为漏了记录。
    """
    where = [] if include_resolved else [RpAiApiError.resolved_at.is_(None)]
    total = db.execute(
        select(func.count()).select_from(RpAiApiError).where(*where)
    ).scalar_one()
    rows = db.execute(
        select(RpAiApiError)
        .where(*where)
        .order_by(RpAiApiError.last_seen.desc())
        .limit(limit)
    ).scalars().all()
    return ApiErrorList(items=[ApiErrorRead.of(r) for r in rows], total=total)


@router.post("/{error_id}/diagnose", response_model=ApiErrorRead, summary="立刻重跑诊断")
def diagnose_now(
    error_id: int,
    db: Session = Depends(get_db),
    _eid: str = Depends(_gate),
) -> ApiErrorRead:
    """同步调判定器再返回。

    同步而不是丢后台：点这个按钮的人正看着页面等结论，而后台任务要么需要一个轮询接口、
    要么需要一个任务表，为一次几秒的调用不值得。上限由 ``error_diagnosis_timeout`` 兜住。
    """
    row = _get_or_404(db, error_id)
    api_errors.diagnose(db, row)
    return ApiErrorRead.of(row)


@router.post("/{error_id}/resolve", response_model=ApiErrorRead, summary="标记已处理")
def resolve(
    error_id: int,
    db: Session = Depends(get_db),
    eid: str = Depends(_gate),
) -> ApiErrorRead:
    """标记已处理，默认列表里不再出现。

    不删行：删掉之后同一个指纹再来会被当成全新问题。而且**再次发生会自动撤销这个标记**
    （见 ``services/api_errors._touch``）——「我看过了」的问题又回来了，是必须重新看见的信号。
    """
    row = _get_or_404(db, error_id)
    if row.resolved_at is None:
        row.resolved_at = func.now()
        row.resolved_by = eid
        db.commit()
        db.refresh(row)
    return ApiErrorRead.of(row)
