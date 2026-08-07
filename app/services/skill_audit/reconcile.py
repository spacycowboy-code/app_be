"""Recompute the audit projection from the facts.

The facts are the audit events (``rp_ai_skill_audit_record`` + its findings) and
the review decision, which lives in GitLab as the state of the review MR. Neither
is edited here.

What this module owns — and is the ONLY writer of — is the projection:

* ``rp_ai_skill.status``  the single lifecycle gate the API already exposes:
      pass verdict            -> approved   (published)
      suspect verdict         -> pending    (waiting for the review MR)
      review MR merged        -> approved
      review MR closed        -> rejected
* ``rp_ai_skill.audit_state`` the latest verdict for the current version, kept
  for display ("why is this pending") and admin filtering. Never a gate.

Because both are derived, they can be rebuilt at any time; a projection that
cannot be rebuilt from the facts would be a second source of truth.

Run it on a schedule (or after a review): ``python -m app.reconcile``.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import RpAiSkillAuditRecord
from app.models.skill import RpAiSkill
from app.services import gitlab_mr

log = logging.getLogger("skill_audit.reconcile")


def latest_record(db: Session, skill_id: str) -> RpAiSkillAuditRecord | None:
    return db.scalars(
        select(RpAiSkillAuditRecord)
        .where(RpAiSkillAuditRecord.skill_id == skill_id)
        .order_by(RpAiSkillAuditRecord.created_at.desc(),
                  RpAiSkillAuditRecord.id.desc())
    ).first()


def reconcile_skill(db: Session, skill: RpAiSkill) -> str | None:
    """Recompute one skill's projection. Returns the new status when it changed."""
    record = latest_record(db, skill.id)
    if record is None:
        return None

    # Display cache: always the latest verdict for the current version.
    skill.audit_state = record.state

    if record.state == "pass":
        new_status = "approved"
    elif record.state == "suspect":
        state = gitlab_mr.mr_state(record.mr_url) if record.mr_url else None
        if state == "merged":
            new_status = "approved"
        elif state == "closed":
            new_status = "rejected"
        else:
            # Still open, or unknown (no token / network): leave it pending
            # rather than guess. Pending keeps it hidden, which fails closed.
            new_status = "pending"
    else:
        # A danger verdict never produces a skill row; if one exists it predates
        # the audit, so leave the lifecycle alone.
        return None

    if skill.status != new_status:
        skill.status = new_status
        return new_status
    return None


def reconcile_all(db: Session) -> dict:
    """Recompute every skill that has an audit record. Idempotent."""
    changed: dict[str, str] = {}
    skills = db.scalars(
        select(RpAiSkill).where(RpAiSkill.audit_state.is_not(None))
    ).all()
    for skill in skills:
        new_status = reconcile_skill(db, skill)
        if new_status:
            changed[skill.id] = new_status
    db.commit()
    if changed:
        log.info("reconciled %d skill(s): %s", len(changed), changed)
    return {"examined": len(skills), "changed": changed}
