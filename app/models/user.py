"""User model: rp_ai_portal.

Stores a portal user keyed by EID, plus links to the skills they have
uploaded and the skills they have collected (favorited).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.favorite import RpAiUserFavorite
    from app.models.skill import RpAiSkill

#: How a row in this table came to exist.
#:
#: ``login`` is the only way there was: :func:`app.routers.auth._upsert_user`
#: inserts on first sign-in, which is why the usage report can read a row here
#: as "this person has used the portal" (``ever_signed_in`` in
#: ``services/usage_report/figures.py``).
#:
#: ``import`` breaks that equivalence, and the column exists to keep the report
#: honest about it. An import registers work by someone who has never signed in,
#: and the row is needed for two things that both read this table: the card's
#: org-group badge (``services/card_group.groups_for_eids``) and placing that
#: person's contributions in a department (``build_people`` in
#: ``services/usage_report/render.py``). Neither of those is a claim about
#: signing in — only ``ever_signed_in`` is, so only ``ever_signed_in`` filters.
PORTAL_ROW_ORIGINS = ("login", "import")


class RpAiPortal(TimestampMixin, Base):
    __tablename__ = "rp_ai_portal"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Ericsson signum / EID — the natural key used by the frontend for auth.
    eid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    department: Mapped[str | None] = mapped_column(String(64))
    # See PORTAL_ROW_ORIGINS. The server default is what makes this safe to add
    # to a populated table without a backfill: every row that predates the
    # column was created on a sign-in, so 'login' is not a guess.
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="login", server_default="login"
    )

    # Skills this user uploaded (RpAiSkill.uploader_eid -> rp_ai_portal.eid).
    uploaded_skills: Mapped[list["RpAiSkill"]] = relationship(
        back_populates="uploader",
        primaryjoin="RpAiPortal.eid == RpAiSkill.uploader_eid",
        viewonly=True,
    )
    # Skills this user collected/favorited.
    favorites: Mapped[list["RpAiUserFavorite"]] = relationship(
        back_populates="user",
        primaryjoin="RpAiPortal.eid == RpAiUserFavorite.eid",
        cascade="all, delete-orphan",
    )
