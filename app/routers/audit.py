"""Audit maintenance and health endpoints.

``POST /api/audit/reconcile`` recomputes the audit projection. It exists so a
GitLab webhook can shorten the delay between a reviewer merging a review MR and
the skill going live; it runs exactly the same convergence function as the
periodic sweep, so the two can never diverge.

``GET /api/audit/health`` counts audit outcomes over a window. Every rule in this gate fails
closed, which means each failure of the audit *itself* is paid for by a reviewer's attention.
One such failure is an incident; a steady stream is our outage — and on a single record the two
look identical. This is the query that tells them apart.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.audit import RpAiSkillAuditRecord
from app.services import audit_engine
from app.services.skill_audit.reconcile import reconcile_all

router = APIRouter(prefix="/api/audit", tags=["audit"])
settings = get_settings()


@router.post("/reconcile")
def reconcile(
    db: Session = Depends(get_db),
    x_gitlab_token: str | None = Header(default=None),
) -> dict:
    """Trigger a reconciliation sweep. Guarded by the configured webhook secret."""
    expected = settings.audit_webhook_token
    if not expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Webhook trigger is disabled (no secret configured)")
    if x_gitlab_token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook token")
    return reconcile_all(db)


@router.get("/health")
def health(
    db: Session = Depends(get_db),
    days: int = Query(7, ge=1, le=90, description="Window in days"),
) -> dict:
    """How the audit itself is doing over the last ``days``.

    The numbers that matter, and why each is here rather than derivable from the states:

    * ``by_coverage`` — how often content reached a reviewer without having been fully
      examined. That is the cost the gate transfers to a person.
    * ``by_cause`` / ``platform_failures`` — of those, how many were our fault. This is the
      one that turns "a reviewer complained" into a number, and it is the reason ``cause`` is
      a column instead of a phrase inside ``reason``.
    * ``unclassified`` — records written before the two columns existed, or by a path that
      forgot to set them. Counted separately rather than folded into a bucket, because a
      growing number here means the measurement itself is degrading.
    """
    since = datetime.utcnow() - timedelta(days=days)
    rows = db.execute(
        select(RpAiSkillAuditRecord.state, RpAiSkillAuditRecord.coverage,
               RpAiSkillAuditRecord.cause, func.count())
        .where(RpAiSkillAuditRecord.created_at >= since)
        .group_by(RpAiSkillAuditRecord.state, RpAiSkillAuditRecord.coverage,
                  RpAiSkillAuditRecord.cause)
    ).all()

    by_state: dict[str, int] = {}
    by_coverage: dict[str, int] = {}
    by_cause: dict[str, int] = {}
    unclassified = 0
    for state, coverage, cause, count in rows:
        by_state[state or "unknown"] = by_state.get(state or "unknown", 0) + count
        if coverage:
            by_coverage[coverage] = by_coverage.get(coverage, 0) + count
        else:
            unclassified += count
        if cause:
            by_cause[cause] = by_cause.get(cause, 0) + count

    total = sum(by_state.values())
    # 归属分类由引擎发布(GET /v1/vocabulary),门户不留副本。取不到时**说自己算不出来**,而不是
    # 报一个偏小的数字 —— 那种数字看起来像"平台很稳定",恰好在链路有问题时最容易误导人。
    platform = audit_engine.platform_owned(
        [c for c, n in by_cause.items() for _ in range(n)])
    not_examined = by_coverage.get(audit_engine.COVERAGE_NONE, 0)
    return {
        "windowDays": days,
        "total": total,
        "byState": by_state,
        "byCoverage": by_coverage,
        "byCause": by_cause,
        # Named plainly so a dashboard does not have to re-derive the meaning: of everything
        # audited in the window, how much failed because of us, and how much reached a human
        # with nothing examined at all.
        "platformFailures": platform,
        "platformFailureRate": (round(platform / total, 4) if platform is not None and total
                                else None if platform is None else 0.0),
        # 分类表取不到时上面两个是 null。前端要按"算不出来"显示,不要显示成 0。
        "platformFailuresUnavailable": platform is None,
        "notExamined": not_examined,
        "unclassified": unclassified,
    }
