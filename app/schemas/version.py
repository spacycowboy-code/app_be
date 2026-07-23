"""Pydantic schemas for skill version history (rp_ai_skill_version)."""
from datetime import datetime

from app.schemas.common import CamelModel
from app.schemas.skill import SkillRead
from app.schemas.audit import AuditFinding


class SkillVersionRead(CamelModel):
    id: int
    skill_id: str
    version: str
    commit_sha: str | None = None
    tag: str | None = None
    uploader_eid: str | None = None
    artifact_name: str | None = None
    notes: str | None = None
    created_at: datetime


class SkillUploadResult(CamelModel):
    """Response returned by the upload endpoint."""

    skill: SkillRead
    version: str
    commit_sha: str | None = None
    tag: str | None = None
    storage_path: str
    pushed: bool
    # Audit outcome so the wizard can show state + findings (no score).
    audit_state: str | None = None      # pass | suspect
    mr_url: str | None = None           # set when suspect -> review MR
    reference: str | None = None
    findings: list[AuditFinding] = []
