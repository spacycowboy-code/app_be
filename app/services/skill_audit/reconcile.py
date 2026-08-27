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

**一个例外,而它是这套投影里最重要的一条:** 可疑结论落在一次"要顶掉已发布内容"的更新上时
(``record.replaces_published``),它完全不碰这两个字段。那种情况下线上那一版没有被任何人质疑过,
被质疑的是还没发布的新内容;它停在 ``rp_ai_skill_pending_update``,MR 合了才搬上来、关了就丢掉。
少了这条,一次被拦下的更新会让一个已经在用的技能从市场上消失 —— 而代价落在所有使用者身上,不是
提交更新的那个人。

Because both are derived, they can be rebuilt at any time; a projection that
cannot be rebuilt from the facts would be a second source of truth.

Run it on a schedule (or after a review): ``python -m app.reconcile``.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import RpAiSkillAuditRecord
from app.models.pending_update import RpAiSkillPendingUpdate
from app.models.skill import RpAiSkill
from app.services import gitlab_mr
from app.services.skill_audit import pending

log = logging.getLogger("skill_audit.reconcile")


def record_for_published(db: Session, skill: RpAiSkill) -> RpAiSkillAuditRecord | None:
    """描述**当前发布内容**的那条审计记录。

    首选技能行上记着的 ``audit_ref``:哪条结论对应现在这一版是一个被写下来的事实,不是从时间戳推出
    来的猜测 —— 记录的 created_at 只精确到秒,同一秒里的两条谁在后是不确定的,而 id 是随机 uuid。

    老数据没有这一格,退回"按时间取最新一条"。这时要排除 ``replaces_published`` 的记录:它们说的是
    线上那一版之外的另一份内容(还在复核、被否了,或者被后来一次直接发布的写入顶掉了)。少了这道
    过滤,一次被拦下的更新会决定一个已发布技能的 status —— 那正是"更新一触发安全问题,旧 skill 就
    不见了"的来源。
    """
    if skill.audit_ref:
        rec = db.get(RpAiSkillAuditRecord, skill.audit_ref)
        if rec is not None:
            return rec
    return latest_record(db, skill.id)


def latest_record(db: Session, skill_id: str) -> RpAiSkillAuditRecord | None:
    return db.scalars(
        select(RpAiSkillAuditRecord)
        .where(RpAiSkillAuditRecord.skill_id == skill_id)
        .where((RpAiSkillAuditRecord.replaces_published.is_(False))
               | (RpAiSkillAuditRecord.replaces_published.is_(None)))
        .order_by(RpAiSkillAuditRecord.created_at.desc(),
                  RpAiSkillAuditRecord.id.desc())
    ).first()


def reconcile_skill(db: Session, skill: RpAiSkill) -> str | None:
    """Recompute one skill's projection. Returns the new status when it changed."""
    # 先处置待复核的更新:它压在线上那一版之外,答案在它自己那一行上(复核 MR 的状态),而不在
    # 审计记录的先后顺序里 —— 记录的时间戳只精确到秒,同一秒里两条谁在前是不确定的。
    row = pending.awaiting_review(db, skill.id)
    if row is not None:
        state = gitlab_mr.mr_state(row.mr_url) if row.mr_url else None
        if state == "merged":
            pending.publish(db, skill, row)
            return "approved"
        if state == "closed":
            pending.reject(db, row)
        # 还开着 / 问不出来:什么都不做。线上那一版继续发布着 —— 这是这里唯一安全的默认值,
        # 因为下架它的代价落在所有使用者身上,而不是提交更新的那个人身上。

    record = record_for_published(db, skill)
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
        select(RpAiSkill).where(
            RpAiSkill.audit_state.is_not(None)
            # 待复核的更新也要收:官方导入的技能 audit_state 是空的,而它同样可以被编辑成一次
            # 待复核的更新 —— 只看 audit_state 的话,那份更新的 MR 合了也永远搬不上去。
            | RpAiSkill.id.in_(select(RpAiSkillPendingUpdate.skill_id))
        )
    ).all()
    for skill in skills:
        new_status = reconcile_skill(db, skill)
        if new_status:
            changed[skill.id] = new_status
    db.commit()
    if changed:
        log.info("reconciled %d skill(s): %s", len(changed), changed)
    return {"examined": len(skills), "changed": changed}
