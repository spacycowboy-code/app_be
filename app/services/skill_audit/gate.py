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

And one rule that is not about the content at all: an audit that could not run
cannot change what is already published. Failing closed is right when nothing is
live yet — the cost lands on the person submitting. When something *is* live, the
same reflex takes a working skill off the market because a service was down, and
the cost lands on everyone using it. That is a worse outcome than waiting.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.audit import RpAiSkillAuditRecord, RpAiSkillFinding
from app.models.user import RpAiPortal
from app.services.skill_audit.types import AuditResult, Finding


@dataclass
class WriteDecision:
    """What the audit outcome allows, resolved once and passed around."""

    audit: AuditResult
    ref: str                       # shown to the author; keys the audit record
    review_branch: str | None      # set when the content must not land live
    # Whether this write would replace content that is already published.
    replaces_published: bool = False

    @property
    def blocked(self) -> bool:
        return self.audit.state == "danger"

    @property
    def unavailable(self) -> bool:
        """The audit could not be established, over content that is already live.

        The caller must change nothing and ask for a retry. Anywhere else an
        unusable verdict routes to review, which fails closed at the submitter's
        expense; here it would fail closed at the expense of every user of a skill
        that was fine a minute ago.
        """
        return self.audit.verdict == "error" and self.replaces_published

    @property
    def needs_review(self) -> bool:
        return self.audit.state == "suspect"

    @property
    def status(self) -> str:
        """The skill's lifecycle field. Only a pass is publicly visible."""
        return "approved" if self.audit.state == "pass" else "pending"


def decide(*, audit: AuditResult, skill_id: str | None = None,
           ref: str | None = None, replaces_published: bool = False) -> WriteDecision:
    ref = ref or uuid.uuid4().hex
    branch = None
    if audit.state == "suspect":
        # One branch per audit, not per skill: two reviews of the same skill are
        # two different pieces of content and must not overwrite each other.
        branch = f"skill-audit/{skill_id or 'new'}-{ref[:8]}"
    return WriteDecision(audit=audit, ref=ref, review_branch=branch,
                         replaces_published=replaces_published)


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
        content_sha=audit.content_sha,
        reason=audit.reason or None,
        coverage=audit.coverage or None,
        cause=audit.cause or None,
        scanner_version=audit.scanner_version,
        policy_fingerprint=audit.policy_fingerprint,
        analyzers_used=",".join(audit.analyzers_used) or None,
        incomplete=",".join(audit.incomplete) or None,
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
                rule_id=f.rule_id or None,
                aitech=f.aitech,
                remediation=f.remediation,
            )
        )
    db.add(rec)

def _uploader_label(db: Session, eid: str | None) -> str | None:
    """How to name the uploader to a human: display name plus EID when known.

    The EID alone is what a reviewer has to look up; the display name alone is
    ambiguous. Both, or the EID if the user row has no display name yet.
    """
    if not eid:
        return None
    # The user row's primary key is an autoincrement id, not the EID, so this is a
    # lookup by column — db.get() would silently return None and every review MR
    # would name a bare EID.
    user = db.query(RpAiPortal).filter(RpAiPortal.eid == eid).first()
    name = user.display_name if user else None
    return f"{name} ({eid})" if name else eid


# How long a verdict may be reused for identical content. Long enough that a person can
# read the report and press publish; short enough that a rules or scanner update takes
# effect without waiting.
REUSE_TTL_SECONDS = 600


def recent_audit(db: Session, content_sha: str, *, ttl: int = REUSE_TTL_SECONDS) -> AuditResult | None:
    """A fresh verdict for exactly this content, if one was already reached.

    evaluate and publish audit the same bytes moments apart, and an audit costs tens of
    seconds. A process-local cache cannot cover that once the API runs more than one
    replica: the two calls may land on different ones. The record is the shared fact, so
    it is what gets reused — and reusing it also means the two calls cannot disagree.
    """
    if not content_sha:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl)
    rec = (
        db.query(RpAiSkillAuditRecord)
        .filter(RpAiSkillAuditRecord.content_sha == content_sha)
        .filter(RpAiSkillAuditRecord.created_at >= cutoff)
        .order_by(RpAiSkillAuditRecord.created_at.desc())
        .first()
    )
    if rec is None:
        return None
    return AuditResult(
        state=rec.state,
        reason=rec.reason or "",
        content_sha=rec.content_sha,
        findings=[
            Finding(source=f.source, category=f.category, severity=f.severity,
                    evidence=f.evidence, detail=f.detail, file=f.file, line=f.line,
                    remediation=f.remediation, rule_id=f.rule_id or "", aitech=f.aitech)
            for f in rec.findings
        ],
        judge_model=rec.judge_model,
        scanner_version=rec.scanner_version,
        policy_fingerprint=rec.policy_fingerprint,
        analyzers_used=(rec.analyzers_used or "").split(",") if rec.analyzers_used else [],
        incomplete=(rec.incomplete or "").split(",") if rec.incomplete else [],
    )
