"""Version history for an uploaded market item: rp_ai_market_item_version.

One row per upload of an agent package. The content itself lives in the item's
own GitLab project (under the user-agents group) — this table records the
lineage: which version, which commit and tag hold it, who uploaded it, and what
the audit said about *that* upload.

Deliberately a table of its own rather than a shared, polymorphic version table
with ``owner_type``: skills already have ``rp_ai_skill_version`` with a real
foreign key and cascade, and a polymorphic table cannot keep either. Two narrow
tables that the database can enforce beat one wide table that it cannot.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.market import RpAiMarketItem


class RpAiMarketItemVersion(TimestampMixin, Base):
    __tablename__ = "rp_ai_market_item_version"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("rp_ai_market_item.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    # Commit and tag in the item's GitLab project that hold this version.
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    tag: Mapped[str | None] = mapped_column(String(128))
    # Who uploaded this version. The GitLab side can only ever name the group's
    # bot, so this column is the portal's answer to "whose package is this".
    uploader_eid: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("rp_ai_portal.eid", ondelete="SET NULL"), index=True
    )
    # Original filename of the uploaded zip, and its size, so the review page
    # can state what was handed over without re-fetching the archive.
    artifact_name: Mapped[str | None] = mapped_column(String(256))
    artifact_bytes: Mapped[int | None] = mapped_column()

    # What the package declares about itself (launch command, runtime, keep
    # globs), as confirmed by the uploader. This is the source the install
    # prompt reads: the copy written into the package is for whoever takes the
    # zip elsewhere, and cannot be trusted to still match after an edit in
    # GitLab.
    metadata_json: Mapped[dict | None] = mapped_column(JSON)

    # The audit outcome for this exact upload: state, coverage, findings, which
    # analyzers ran, which did not, the policy fingerprint and the judge's raw
    # answer. Stored per version rather than per item because "what did we know
    # when this went in" is a question about a version, and the next upload must
    # not overwrite the answer.
    audit_json: Mapped[dict | None] = mapped_column(JSON)

    notes: Mapped[str | None] = mapped_column(Text)

    item: Mapped["RpAiMarketItem"] = relationship(back_populates="versions")
