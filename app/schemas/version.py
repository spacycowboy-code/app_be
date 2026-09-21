"""Pydantic schemas for skill version history (rp_ai_skill_version)."""
from datetime import datetime

from app.schemas.common import CamelModel
from app.schemas.skill import SkillRead
from app.schemas.audit import AuditCheck, AuditFinding


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
    # ``version`` 是从哪来的：``declared`` = SKILL.md frontmatter 里的 `version`，
    # ``auto`` = 平台自增，``unchanged`` = 内容没变、没有发新版本。
    version_source: str = "auto"
    # 声明值被跳过时给作者的一句话；直接采用或未声明时为空串。
    version_note: str = ""
    # Audit outcome so the wizard can show state + findings (no score).
    audit_state: str | None = None      # pass | suspect
    # Why, in one line. A suspect verdict does not always have findings — "a layer did not
    # finish" produces none — so without this the wizard can only say "needs review".
    reason: str | None = None
    coverage: str | None = None
    cause: str | None = None
    mr_url: str | None = None           # set when suspect -> review MR
    reference: str | None = None
    checks: list[AuditCheck] = []
    findings: list[AuditFinding] = []
