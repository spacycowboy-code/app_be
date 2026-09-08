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
from app.services.card_group import groups_for_eids
from app.services.download_counts import MARKET, SKILL, unique_downloads


def attach_market_stats(db: Session, items: list[RpAiMarketItem]) -> None:
    """Attach deduplicated-by-EID favorite/download counts onto each item,
    plus the creator's org group.
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
    # 去重规则只有一处 —— services/download_counts。这里原本是本文件自己的
    # count(distinct eid)，而技能那边是另一套算法，同一个人下载同一个东西在两个市场
    # 得出不同的数。顺带修掉 distinct 整体忽略 NULL 造成的匿名下载算 0 次。
    dls = unique_downloads(db, MARKET, ids)
    groups = groups_for_eids(db, {it.creator for it in items if it.creator})
    for it in items:
        it.favorite_count = int(fav.get(it.id, 0))
        it.download_count = int(dls.get(it.id, 0))
        it.group = groups.get((it.creator or "").strip().upper())


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


def attach_skill_groups(db: Session, skills: list[RpAiSkill]) -> None:
    """把上传者的组（地址 · 大部门 · 小部门）挂到每个 skill 上。

    按 ``uploader_eid`` 而不是 ``author``:官方导入的那批 ``author`` 是 "Anthropic" 这类标签,
    不是人、也没有办公地点。那些 skill 没有组,而这就是诚实的答案。

    单独抽出来是因为有两条路要用它:列表走 ``attach_skill_stats``,而单条详情端点自己算那两个
    统计字段、不调 ``attach_skill_stats``。原先的写法是路由里另有一份实现,只在详情那条路上被
    调用 —— 列表端点从没调过,于是 Skill Market 每张卡片的组徽标都是空的,详情页却有,而这个
    差别没有任何报错。
    """
    groups = groups_for_eids(db, {s.uploader_eid for s in skills if s.uploader_eid})
    for s in skills:
        # 大小写统一成大写:``uploader_eid`` 的历史数据里 ``ezhgmio`` 和 ``EZHGMIO`` 都有。
        s.group = groups.get((s.uploader_eid or "").strip().upper())


def attach_skill_stats(db: Session, skills: list[RpAiSkill]) -> None:
    """Attach the deduplicated like count, the content-change timestamp, and the
    uploader's org group.

    Favorites are unique per (eid, skill) by table constraint, so the row count
    is the like count.

    组也在这里挂,和 ``attach_market_stats`` 一致 —— 两个市场做同一件事就该走同一条路,否则
    只会在其中一条上退化。
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
    # 下载量从事件表现算，**盖掉 ``rp_ai_skill.downloads`` 那个累计列**。那一列是每次
    # 下载 +1 的次数，不是人数；而且它和事件表已经漂了（线上 399 vs 356）。真相在事件表。
    dls = unique_downloads(db, SKILL, ids)
    for s in skills:
        s.favorite_count = int(counts.get(s.id, 0))
        s.content_updated_at = content_updated_at(s, *version_stats.get(s.id, (0, None)))
        s.downloads = int(dls.get(s.id, 0))
    attach_skill_groups(db, skills)
