"""Audit maintenance endpoint.

``POST /api/audit/reconcile`` recomputes the audit projection. It exists so a
GitLab webhook can shorten the delay between a reviewer merging a review MR and
the skill going live; it runs exactly the same convergence function as the
periodic sweep, so the two can never diverge.
"""
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
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
