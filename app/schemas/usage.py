"""Schemas for usage recording, personal history, and the weekly report."""
from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_serializer, field_validator

from app.models.usage_event import (
    ALLOWED_OBJECT_TYPES,
    ALLOWED_OUTCOMES,
    ALLOWED_VERBS,
)
from app.schemas.common import CamelModel, as_utc

# --- Writing ----------------------------------------------------------------


class UsageEventCreate(CamelModel):
    """One action the browser reports after it happened.

    There is no ``eid`` field, and that is deliberate: the actor is taken from
    the session token. Accepting it here would let any signed-in user attribute
    activity to a colleague, which is the same mistake the upload endpoint had
    (see ``tests/test_upload_actor.py``).
    """

    verb: str
    object_type: str
    object_id: str = Field(min_length=1, max_length=128)
    object_name: str | None = Field(default=None, max_length=255)
    source: str | None = Field(default=None, max_length=32)
    #: Only meaningful for ``tool_run``; rejected for verbs that cannot have an
    #: outcome, so a report never shows a success rate computed from opens.
    outcome: str | None = None
    #: How long the user waited, in milliseconds. Clamped rather than trusted:
    #: a tab left suspended can report hours, which would wreck any average.
    duration_ms: int | None = Field(default=None, ge=0, le=3_600_000)

    @field_validator("verb")
    @classmethod
    def _known_verb(cls, value: str) -> str:
        if value not in ALLOWED_VERBS:
            raise ValueError(f"unknown verb: {value!r}")
        return value

    @field_validator("object_type")
    @classmethod
    def _known_object_type(cls, value: str) -> str:
        if value not in ALLOWED_OBJECT_TYPES:
            raise ValueError(f"unknown objectType: {value!r}")
        return value

    @field_validator("outcome")
    @classmethod
    def _known_outcome(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_OUTCOMES:
            raise ValueError(f"unknown outcome: {value!r}")
        return value


# --- Personal history -------------------------------------------------------


class DownloadHistoryEntry(CamelModel):
    """One download the user made, newest first in the list.

    ``objectType`` distinguishes the two sources, because a skill and a market
    item can share a slug.

    A known gap, inherited from the tables this reads: both download logs
    reference their object with ``ON DELETE CASCADE``, so deleting a skill or an
    item also deletes everybody's record of having downloaded it. The history is
    therefore "what you downloaded that still exists", and an entry can
    disappear through no action of the user's. Fixing it properly means
    dropping the cascade, which is a change to tables Grafana already reads, so
    it is left alone here and written down instead.
    """

    object_type: str
    object_id: str
    name: str | None = None
    version: str | None = None
    downloaded_at: datetime

    @field_serializer("downloaded_at")
    def _utc(self, value: object) -> object:
        return as_utc(value)


class ToolUsage(CamelModel):
    """How often one tool was used by the signed-in user in the window.

    ``runs`` and ``opens`` are separate because they are different evidence:
    a run finished in the portal's sight, an open only means the user was sent
    to the tool. Summing them would produce a number that reads like "uses" but
    is not comparable between a built-in page and an external service.
    """

    object_type: str
    object_id: str
    name: str | None = None
    runs: int = 0
    opens: int = 0
    #: Failed runs, included in ``runs``. Surfaced so "used a lot" and "fought
    #: with it a lot" can be told apart.
    failed_runs: int = 0
    last_used_at: datetime

    @field_serializer("last_used_at")
    def _utc(self, value: object) -> object:
        return as_utc(value)


# --- Weekly report ----------------------------------------------------------


class ObjectCount(CamelModel):
    """A named thing and how many times it was counted."""

    object_id: str
    name: str | None = None
    count: int


class LabelCount(CamelModel):
    """A bucket label and its size (audit reasons, groups, outcomes)."""

    label: str
    count: int


class ToolActivity(CamelModel):
    """Per-tool totals for the week, runs and opens kept apart."""

    object_type: str
    object_id: str
    name: str | None = None
    runs: int = 0
    failed_runs: int = 0
    opens: int = 0


class UsageReport(CamelModel):
    """Aggregates for a weekly write-up. No per-person figures.

    Everything here is a count of *things* or of *distinct people* — never a
    named person's activity. That is a deliberate limit, not an oversight:

    * A per-person ranking of a tool's usage measures how much someone had to
      use a tool, not how much they achieved, so it is a poor proxy for
      contribution and a good way to get gamed. Once people know sign-ins are
      reported, sign-ins stop measuring anything.
    * Using activity records to appraise employees is a different purpose from
      running the platform, and in the EU that difference is what needs a legal
      basis and notice.

    Naming individuals is still legitimate for two things — inviting early
    adopters to an interview, and asking heavy users for feedback — but those
    are lists to talk to, not scores to compare, so they are not part of this
    response.
    """

    since: datetime
    until: datetime
    #: False while the window is still running, so a partial week is not read
    #: as a quiet one.
    complete: bool

    # Adoption
    active_people: int
    #: People whose first recorded sign-in falls inside the window.
    new_people: int
    #: Distinct groups (mapped from department) with any activity.
    active_groups: int
    #: Activity per group, so "who is this landing with" has an answer that is
    #: about teams rather than individuals.
    by_group: list[LabelCount]

    # Assets
    skills_added: int
    items_added: int
    #: Most-downloaded assets in the window.
    top_downloads: list[ObjectCount]
    #: Published assets that nobody downloaded in the window. More actionable
    #: than the top list: it is the candidate list for removal.
    unused_assets: int

    # Depth of use
    downloads: int
    #: Downloads by distinct people, so one person pulling ten copies does not
    #: look like ten adopters.
    distinct_downloaders: int
    install_commands_copied: int
    #: People who were also active in the previous week.
    returning_people: int

    # Quality signals — what to fix next week
    #: Uploads the audit refused, and why.
    audits_blocked: int
    audits_total: int
    audit_reasons: list[LabelCount]
    #: Tool runs that ended in an error.
    failed_runs: int

    tools: list[ToolActivity]

    @field_serializer("since", "until")
    def _utc(self, value: object) -> object:
        return as_utc(value)
