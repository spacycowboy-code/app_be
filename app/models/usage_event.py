"""Portal usage event log: rp_ai_usage_event.

One row per deliberate action that no existing table already records.

**Why a new table instead of one generic event table for everything.** Sign-ins,
skill downloads, market downloads and favorites already have purpose-built
tables (``rp_ai_login_event``, ``rp_ai_skill_download``,
``rp_ai_market_download``, ``rp_ai_user_favorite``). They carry real foreign
keys, so deleting a skill clears its history, and ``grafana/dashboards/
skill-metrics.json`` queries ``rp_ai_skill_download`` directly. Folding them
into a generic log would mean a data migration, losing the FKs, and rewriting
the dashboards — for no new answer. This table therefore holds only what has
nowhere to live yet: tool usage and install-command copies. The read side
(see ``routers/usage.py``) joins across both kinds.

**Why ``department`` is copied in rather than joined at read time.** A person's
department changes, and so does the department→group mapping (this is exactly
why the group is not a JWT claim — see the frontend's ``GroupService``). Joining
at read time would let a reorganisation silently rewrite *last quarter's*
"which group used this most", which is the one thing a report is supposed to
pin down. The value stored here is the department as it was when the event
happened.

**What is deliberately not stored.** No query text, no document reference, no
IP, no user agent, no page dwell time. A tool's search terms can name an
unreleased product, and none of it is needed to answer "which tools get used".
Only whether an action happened, to what, and whether it worked.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# ---------------------------------------------------------------------------
# Verbs. The set is closed: an unknown verb is rejected by the write endpoint
# rather than stored, because a typo in a caller would otherwise create a
# silent second category that every aggregate then under-reports.
# ---------------------------------------------------------------------------

#: A tool ran and the portal saw how it ended. Carries ``outcome`` and
#: ``duration_ms``. Today only the built-in tool pages qualify: the browser
#: issues the request and waits for the result.
VERB_TOOL_RUN = "tool_run"

#: A market item was opened — the user was sent to its ``serviceUrl`` /
#: ``repoUrl``. Intent to use, with **no** outcome: once the browser leaves,
#: the portal sees nothing more. Kept apart from ``tool_run`` on purpose, so a
#: report cannot claim "invoked N times" when the evidence is "opened N times".
VERB_TOOL_OPEN = "tool_open"

#: An install instruction was copied for a code item. A stronger signal of real
#: use than a download, which can be idle curiosity.
VERB_INSTALL_COPIED = "install_command_copied"

ALLOWED_VERBS = frozenset({VERB_TOOL_RUN, VERB_TOOL_OPEN, VERB_INSTALL_COPIED})

#: Outcomes for ``tool_run``. ``error`` covers a failed call of any kind; the
#: reason is not stored here because it belongs to the tool's own logs.
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
ALLOWED_OUTCOMES = frozenset({OUTCOME_OK, OUTCOME_ERROR})

#: What the ``object_id`` points at, so two tools with the same slug in
#: different namespaces cannot be merged by accident.
OBJECT_TOOL = "tool"          # a built-in tool page, e.g. "product-structure"
OBJECT_MARKET_ITEM = "market_item"
ALLOWED_OBJECT_TYPES = frozenset({OBJECT_TOOL, OBJECT_MARKET_ITEM})


class RpAiUsageEvent(Base):
    __tablename__ = "rp_ai_usage_event"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Who acted, uppercase (see services.eids.normalize_eid). Taken from the
    # session token by the write endpoint, never from the request body.
    #
    # Not a ForeignKey to rp_ai_portal, matching RpAiLoginEvent: usage history
    # should outlive a user row being removed, and recording must never fail
    # because a parent row is missing.
    eid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    # The actor's LDAP department **at the time of the event** (see module
    # docstring). Null when the user row has none. Width matches
    # ``RpAiPortal.department``, which is where the value is copied from.
    department: Mapped[str | None] = mapped_column(String(64))

    # One of ALLOWED_VERBS.
    verb: Mapped[str] = mapped_column(String(32), index=True, nullable=False)

    # One of ALLOWED_OBJECT_TYPES.
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # Slug or id of the thing acted on. Deliberately not a ForeignKey: a market
    # item can be deleted or a tool page retired, and "this tool was used 40
    # times last quarter" must survive that — unlike the download tables, where
    # the history is only interesting while the item exists.
    object_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)

    # Display name as it was, so a report can name a since-deleted item instead
    # of showing a bare slug.
    object_name: Mapped[str | None] = mapped_column(String(255))

    # Which entry point the action came from ("market", "home", "workspace",
    # "tool-page"). Answers "where do people actually start" — the market page
    # and the home page list the same items, and only this tells them apart.
    source: Mapped[str | None] = mapped_column(String(32))

    # For tool_run: one of ALLOWED_OUTCOMES. Null for verbs with no outcome.
    outcome: Mapped[str | None] = mapped_column(String(16))

    # For tool_run: wall-clock duration the *user* waited, measured in the
    # browser. Null for verbs with no outcome. Not the server's processing
    # time — the point is what the person experienced.
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # Written by the DB in its local zone (naive), matching every other table.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (
        # "this person's recent tool use", the workspace's only query here.
        Index("ix_usage_event_eid_created", "eid", "created_at"),
        # "counts per verb over a window", every report query.
        Index("ix_usage_event_verb_created", "verb", "created_at"),
    )
