"""Per-asset counters that list endpoints attach before serialising.

These lived inside ``routers/market.py`` and ``routers/skills.py`` as long as
those were the only list endpoints. The department pages are a second reader of
the same lists, and a card that shows a different like count depending on which
page it is rendered on is a bug nobody would think to look for — so the counting
lives here once rather than being copied per router.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.favorite import RpAiUserFavorite
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.market_favorite import RpAiMarketFavorite
from app.models.skill import RpAiSkill
from app.models.version import RpAiSkillVersion


def attach_market_stats(db: Session, items: list[RpAiMarketItem]) -> None:
    """Attach deduplicated-by-EID favorite/download counts onto each item.

    Favorites are already unique per (eid, item) by table constraint, so the
    row count is the like count. Downloads are raw events, so we count distinct
    EIDs (anonymous/NULL EIDs are excluded from the unique-user count).
    """
    if not items:
        return
    ids = [it.id for it in items]
    fav = dict(
        db.execute(
            select(RpAiMarketFavorite.item_id, func.count(RpAiMarketFavorite.id))
            .where(RpAiMarketFavorite.item_id.in_(ids))
            .group_by(RpAiMarketFavorite.item_id)
        ).all()
    )
    dls = dict(
        db.execute(
            select(
                RpAiMarketDownload.item_id,
                func.count(func.distinct(RpAiMarketDownload.eid)),
            )
            .where(RpAiMarketDownload.item_id.in_(ids))
            .group_by(RpAiMarketDownload.item_id)
        ).all()
    )
    for it in items:
        it.favorite_count = int(fav.get(it.id, 0))
        it.download_count = int(dls.get(it.id, 0))


def content_updated_at(
    skill: RpAiSkill, upload_count: int, latest_upload: datetime | None
) -> datetime | None:
    """When this skill's content last changed, for the Home feed's "Update" rule.

    ``rp_ai_skill_version`` holds one row per upload, which is the only
    authoritative record of a content change — ``updated_at`` on the skill row is
    a bookkeeping column that moves on any write to it.

    * two or more uploads -> the newest one is a real update;
    * exactly one upload -> that upload *is* the creation, so report the creation
      time. Its version row is stamped seconds later than the skill row (separate
      INSERTs, separate ``CURRENT_TIMESTAMP``), and returning that would announce
      every brand-new skill as updated;
    * no version history at all -> the official imports, which never write one.
      Fall back to ``updated_at``: it is trustworthy for them now that the import
      writes a per-folder commit sha rather than the repo-wide HEAD.
    """
    if upload_count >= 2 and latest_upload is not None:
        return latest_upload
    if upload_count == 1:
        return skill.created_at
    return skill.updated_at


def attach_skill_stats(db: Session, skills: list[RpAiSkill]) -> None:
    """Attach the deduplicated like count and the content-change timestamp.

    Favorites are unique per (eid, skill) by table constraint, so the row count
    is the like count.
    """
    if not skills:
        return
    ids = [s.id for s in skills]
    counts = dict(
        db.execute(
            select(RpAiUserFavorite.skill_id, func.count(RpAiUserFavorite.id))
            .where(RpAiUserFavorite.skill_id.in_(ids))
            .group_by(RpAiUserFavorite.skill_id)
        ).all()
    )
    # Upload history per skill: how many uploads, and when the newest landed.
    # The count is what decides whether the content ever changed — a single
    # upload is the creation itself, however many seconds after the skill row
    # its version row happened to be INSERTed (``upload.py`` flushes the skill
    # first, then commits the version row after the audit record and, for a
    # suspect upload, a GitLab round-trip). Comparing the two timestamps
    # directly would mark every brand-new skill as updated.
    version_stats = {
        sid: (int(n), latest)
        for sid, n, latest in db.execute(
            select(
                RpAiSkillVersion.skill_id,
                func.count(RpAiSkillVersion.id),
                func.max(RpAiSkillVersion.created_at),
            )
            .where(RpAiSkillVersion.skill_id.in_(ids))
            .group_by(RpAiSkillVersion.skill_id)
        ).all()
    }
    for s in skills:
        s.favorite_count = int(counts.get(s.id, 0))
        s.content_updated_at = content_updated_at(s, *version_stats.get(s.id, (0, None)))
