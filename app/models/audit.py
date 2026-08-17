"""Skill security-audit models.

Separates *facts* from *derived state*:

* ``RpAiSkillAuditRecord`` — one row per audit run (an upload attempt). Carries
  the derived state (通过/可疑/危险) and, for suspect uploads, the Draft MR url.
  For a *danger* verdict the skill is never stored, so ``skill_id`` is null and
  the record's id is the reference shown to the uploader.
* ``RpAiSkillFinding`` — append-only findings (the evidence). Never mutated.
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

# Derived audit states.  pass=通过  suspect=可疑  danger=危险
AUDIT_STATES = ("pass", "suspect", "danger")
# Finding categories.  Red-line categories force a danger verdict.
RED_LINE_CATEGORIES = (
    "secret",
    "dangerous_instruction",
    "hidden_instruction",
    "prompt_injection",
)


class RpAiSkillAuditRecord(TimestampMixin, Base):
    __tablename__ = "rp_ai_skill_audit_record"

    # UUID hex; also the reference id surfaced to a rejected uploader.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Null when the upload was rejected (danger) and therefore never stored.
    skill_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="SET NULL"), index=True
    )
    skill_name: Mapped[str | None] = mapped_column(String(200))
    uploader_eid: Mapped[str | None] = mapped_column(String(64), index=True)
    # pass | suspect | danger
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    # Semantic judge verdict: block | flag | pass | error
    judge_verdict: Mapped[str | None] = mapped_column(String(16))
    judge_raw: Mapped[str | None] = mapped_column(Text)
    # Which model(s) judged it, and the rules text they were given: both are
    # inputs to the verdict, so the record is only reproducible with them.
    judge_model: Mapped[str | None] = mapped_column(String(120))
    # Hash of exactly what was audited (name + SKILL.md + bundle bytes). Two purposes:
    # it says whether two audits saw the same content, and it lets a second replica reuse
    # a fresh verdict instead of paying for the same scan again — an in-process cache
    # cannot, because evaluate and publish may land on different replicas.
    content_sha: Mapped[str | None] = mapped_column(String(64), index=True)
    # One line saying why this outcome, so the record explains itself.
    reason: Mapped[str | None] = mapped_column(String(300))
    # Kept for records written before the scanner owned the pipeline.
    rules_hash: Mapped[str | None] = mapped_column(String(64))
    # A verdict can only be explained later if the three inputs are known: which model,
    # which scanner build, which policy. Without them a change of rules is
    # indistinguishable from a change of content.
    scanner_version: Mapped[str | None] = mapped_column(String(120))
    policy_fingerprint: Mapped[str | None] = mapped_column(String(120))
    # Which analyzers ran, and which reported they could not finish. Recorded so that a
    # systemic slide into "everything goes to review" is visible as a rate.
    analyzers_used: Mapped[str | None] = mapped_column(Text)
    incomplete: Mapped[str | None] = mapped_column(Text)
    # Draft MR url for suspect uploads (null otherwise / until created).
    mr_url: Mapped[str | None] = mapped_column(String(512))

    findings: Mapped[list["RpAiSkillFinding"]] = relationship(
        back_populates="record", cascade="all, delete-orphan"
    )


class RpAiSkillFinding(Base):
    __tablename__ = "rp_ai_skill_finding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    audit_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("rp_ai_skill_audit_record.id", ondelete="CASCADE"),
        index=True,
    )
    # Producer: detect-secrets | cisco | judge | pipeline
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    # critical | high | medium | low
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    # Verbatim evidence span (secrets are redacted before storage).
    evidence: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    # Where it is, so the record and the review comment can point at it. The path
    # may address a file inside the uploaded bundle (bundle.zip!/scripts/x.py).
    file: Mapped[str | None] = mapped_column(String(512))
    # The scanner's rule id and taxonomy code, and what to change. Persisted because the
    # record is the durable form of the finding: without them it can name a category but
    # not what fired or what the author should do, and neither can be reconstructed.
    rule_id: Mapped[str | None] = mapped_column(String(120))
    aitech: Mapped[str | None] = mapped_column(String(32))
    remediation: Mapped[str | None] = mapped_column(Text)
    line: Mapped[int | None] = mapped_column(Integer)

    record: Mapped["RpAiSkillAuditRecord"] = relationship(back_populates="findings")
