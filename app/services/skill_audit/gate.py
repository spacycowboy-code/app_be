"""Where an audit outcome becomes a write decision.

Every path that puts skill content into the repo — an upload and an edit — asks
this module what to do, and `skill_storage.store` refuses to write without the
answer. The gate used to live in the upload route, which meant the edit route
wrote the same content, to the same repo, with no audit at all: the check was
attached to a route instead of to the act of writing.

What each outcome means:

* ``danger``  nothing is written. The audit record is kept, because "we rejected
              this and why" has to be answerable later.
* ``suspect`` written to a review branch, never to the live branch, and the skill
              stays ``pending`` until a reviewer merges the MR. Content that has
              not been reviewed is not content that is published.
* ``pass``    written live.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.audit import RpAiSkillAuditRecord, RpAiSkillFinding
from app.services.skill_audit.types import AuditResult


@dataclass
class WriteDecision:
    """What the audit outcome allows, resolved once and passed around."""

    audit: AuditResult
    ref: str                       # shown to the author; keys the audit record
    review_branch: str | None      # set when the content must not land live

    @property
    def blocked(self) -> bool:
        return self.audit.state == "danger"

    @property
    def needs_review(self) -> bool:
        return self.audit.state == "suspect"

    @property
    def status(self) -> str:
        """The skill's lifecycle field. Only a pass is publicly visible."""
        return "approved" if self.audit.state == "pass" else "pending"


def decide(*, audit: AuditResult, skill_id: str | None = None,
           ref: str | None = None) -> WriteDecision:
    ref = ref or uuid.uuid4().hex
    branch = None
    if audit.state == "suspect":
        # One branch per audit, not per skill: two reviews of the same skill are
        # two different pieces of content and must not overwrite each other.
        branch = f"skill-audit/{skill_id or 'new'}-{ref[:8]}"
    return WriteDecision(audit=audit, ref=ref, review_branch=branch)


def record(
    db: Session,
    ref: str,
    audit: AuditResult,
    *,
    skill_id: str | None,
    skill_name: str,
    uploader_eid: str | None,
    mr_url: str | None = None,
) -> None:
    """Persist the audit record + its findings (append-only facts)."""
    rec = RpAiSkillAuditRecord(
        id=ref,
        skill_id=skill_id,
        skill_name=skill_name,
        uploader_eid=uploader_eid,
        state=audit.state,
        judge_verdict=audit.verdict,
        judge_raw=audit.raw,
        judge_model=audit.judge_model,
        rules_hash=audit.rules_hash,
        mr_url=mr_url,
    )
    for f in audit.findings:
        rec.findings.append(
            RpAiSkillFinding(
                source=f.source,
                category=f.category,
                severity=f.severity,
                evidence=f.evidence,
                detail=f.detail,
                file=f.file,
                line=f.line,
            )
        )
    db.add(rec)
