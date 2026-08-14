"""Pydantic schemas for skills (rp_ai_skill) and their child records."""
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.common import CamelModel

SkillStatus = Literal["pending", "approved", "rejected"]


# --- child schemas ---------------------------------------------------------
class SkillCaseSchema(CamelModel):
    title: str
    image: str | None = None


class SubSkillSchema(CamelModel):
    name: str
    # ORM attribute is ``description``; frontend JSON key is ``desc``.
    description: str | None = Field(default=None, alias="desc")
    badge: str | None = None
    icon_color: str | None = None
    icon_bg: str | None = None


class InputParamSchema(CamelModel):
    name: str
    type: str | None = None
    required: bool = False
    description: str | None = Field(default=None, alias="desc")


# --- skill schemas ---------------------------------------------------------
class SkillBase(CamelModel):
    """What the author of a skill provides.

    Deliberately does not carry the fields the server derives — rating, downloads,
    review counts, trending, lifecycle status, version, resolved names. They used
    to live here, and because this model is also the input model, a client could
    set its own skill to ``approved`` or write its own rating and download count.
    A field belongs on the input surface only if the person submitting the skill
    is the one who decides it.
    """

    name: str
    description: str | None = None
    category: str | None = None
    category_id: str | None = None
    image: str | None = None
    is_free: bool = True
    tags: list[str] = Field(default_factory=list)
    capability_full: str | None = None
    example_prompt: str | None = None
    example_output: str | None = None
    instructions: str | None = None

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v: object) -> object:
        return v if v is not None else []


class SkillDerived(CamelModel):
    """What the server decides. Read-only from the API's point of view.

    ``status`` is written by exactly three places: the upload gate (from the audit
    outcome), the official import (auto-approved), and the reconciler (from the
    review MR's state). None of them is the client.
    """

    rating: float = 0.0
    downloads: str = "0"
    is_trending: bool = False
    version: str | None = None
    author: str | None = None
    uploader_eid: str | None = None
    # LDAP display name of the uploader (read-only, resolved from the user row).
    uploader_name: str | None = None
    # Name to show as the creator (person's display name, or imported label).
    creator_label: str | None = None
    rating_dist: list[int] | None = None
    review_count: int = 0
    status: SkillStatus = "pending"
    # Why a skill is not public yet: pass | suspect | danger. Without it an admin
    # sees "pending" and cannot tell whether it is waiting on a review or on
    # something else.
    audit_state: str | None = None


class SkillCreate(SkillBase):
    # Client may supply a slug id; if omitted the server generates one.
    id: str | None = None
    cases: list[SkillCaseSchema] = Field(default_factory=list)
    sub_skills: list[SubSkillSchema] = Field(default_factory=list)
    input_params: list[InputParamSchema] = Field(default_factory=list)


class SkillUpdate(CamelModel):
    """The editable surface. Same rule as SkillBase: only what the author decides.

    Extra keys are ignored rather than rejected, so a client that still posts the
    whole skill object (rating, downloads, status) keeps working — those values
    are simply not writable any more.
    """

    name: str | None = None
    description: str | None = None
    category: str | None = None
    category_id: str | None = None
    image: str | None = None
    is_free: bool | None = None
    tags: list[str] | None = None
    capability_full: str | None = None
    example_prompt: str | None = None
    example_output: str | None = None
    instructions: str | None = None
    cases: list[SkillCaseSchema] | None = None
    sub_skills: list[SubSkillSchema] | None = None
    input_params: list[InputParamSchema] | None = None
    # EID of the user performing the edit (recorded in the version history).
    editor_eid: str | None = None
    # Full SKILL.md content — when provided, it is written verbatim as the
    # skill's definition file (name/description are synced from its frontmatter).
    skill_md: str | None = None


class SkillRead(SkillBase, SkillDerived):
    id: str
    cases: list[SkillCaseSchema] = Field(default_factory=list)
    sub_skills: list[SubSkillSchema] = Field(default_factory=list)
    input_params: list[InputParamSchema] = Field(default_factory=list)
    storage_path: str | None = None
    repo_commit: str | None = None
    favorite_count: int = 0
    created_at: datetime
    updated_at: datetime
    # Git sync status of the last write. ``synced`` is None when remote push is
    # disabled or not applicable; False means the change was saved locally but
    # the push to the GitLab remote failed. ``sync_warning`` carries a
    # human-readable message the frontend can surface when synced is False.
    synced: bool | None = None
    sync_warning: str | None = None


class SkillSummary(CamelModel):
    """Lightweight shape for list/market views."""

    id: str
    name: str
    description: str | None = None
    category: str | None = None
    category_id: str | None = None
    image: str | None = None
    rating: float = 0.0
    downloads: str = "0"
    is_trending: bool = False
    is_free: bool = True
    tags: list[str] = Field(default_factory=list)
    author: str | None = None
    uploader_eid: str | None = None
    uploader_name: str | None = None
    creator_label: str | None = None
    favorite_count: int = 0
    status: SkillStatus = "pending"
    # Why it is not public yet, for whoever has to act on the queue.
    audit_state: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
