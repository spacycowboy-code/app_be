"""Department detail endpoints: the nine departments and what each one holds.

Why this is a server endpoint rather than the frontend calling
``/api/market?categoryId=…`` and ``/api/skills?categoryId=…`` twice: the two
existing endpoints answer "what is filed under this group", which is most of the
job, but the page also needs the *set* of departments, the "everything the nine do
not claim" complement, and one visibility rule applied identically across both
kinds. Spreading the department list and the complement across the browser would
put the same table in two places, and the complement in particular cannot be
expressed with the existing filters at all.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.market import RpAiMarketItem
from app.models.skill import RpAiSkill
from app.schemas.department import (
    DepartmentAssets,
    DepartmentMetrics,
    DepartmentSummary,
)
from app.services.asset_stats import attach_market_stats, attach_skill_stats
from app.services.departments import (
    DEPARTMENT_CATEGORY_IDS,
    DEPARTMENTS,
    UNASSIGNED,
    Department,
    department_by_slug,
)
from app.services.eids import acting_eid

router = APIRouter(prefix="/api/departments", tags=["departments"])
settings = get_settings()

#: The market ``item_type`` values this page shows. ``ai-platform`` is excluded
#: on purpose — the page renders skills, agents and tools, and counting a kind it
#: never displays would leave a number nothing on screen adds up to.
_ITEM_KINDS = ("agent", "tool")


def _only_visible(stmt: Select, model: type, owner_column, requester: str | None) -> Select:
    """Restrict a list query to rows the requester may see.

    Same rule as the market and skill lists: published to everyone, plus your own
    while it waits for review. It is repeated here rather than trusted from
    upstream because this is an independent read path — a page that listed
    somebody else's pending upload would be a way around the review gate, not a
    cosmetic bug.
    """
    if requester == settings.audit_admin_eid:
        return stmt
    return stmt.where(or_(model.status == "approved", owner_column == requester))


def _category_filter(stmt: Select, model: type, dept: Department) -> Select:
    """Rows belonging to ``dept``, or the complement for the unassigned page."""
    if dept.slug == UNASSIGNED.slug:
        # NULL and '' both mean "no group was recorded", and neither is caught by
        # a NOT IN comparison — SQL NOT IN against NULL yields NULL, which filters
        # the row out rather than keeping it. Spelled out so the two items that
        # currently have an empty category cannot go missing from the one page
        # whose whole job is to account for them.
        return stmt.where(
            or_(
                model.category_id.is_(None),
                model.category_id == "",
                model.category_id.not_in(sorted(DEPARTMENT_CATEGORY_IDS)),
            )
        )
    return stmt.where(model.category_id == dept.slug)


def _skills_of(db: Session, dept: Department, requester: str | None) -> list[RpAiSkill]:
    stmt = _category_filter(select(RpAiSkill), RpAiSkill, dept)
    stmt = _only_visible(stmt, RpAiSkill, RpAiSkill.uploader_eid, requester)
    skills = list(db.scalars(stmt.order_by(RpAiSkill.name)).all())
    attach_skill_stats(db, skills)
    return skills


def _items_of(
    db: Session, dept: Department, requester: str | None
) -> dict[str, list[RpAiMarketItem]]:
    """The department's market items, bucketed by ``item_type``.

    One query for both kinds rather than one per kind: the counts and the lists
    have to come from the same read, or a concurrent publish can make the header
    disagree with the cards under it.
    """
    stmt = _category_filter(select(RpAiMarketItem), RpAiMarketItem, dept)
    stmt = _only_visible(stmt, RpAiMarketItem, RpAiMarketItem.creator, requester)
    stmt = stmt.where(RpAiMarketItem.item_type.in_(_ITEM_KINDS))
    items = list(db.scalars(stmt.order_by(RpAiMarketItem.name)).all())
    attach_market_stats(db, items)
    buckets: dict[str, list[RpAiMarketItem]] = {kind: [] for kind in _ITEM_KINDS}
    for item in items:
        buckets[item.item_type].append(item)
    return buckets


def _assets(db: Session, dept: Department, requester: str | None) -> DepartmentAssets:
    skills = _skills_of(db, dept, requester)
    buckets = _items_of(db, dept, requester)
    agents = buckets["agent"]
    tools = buckets["tool"]
    return DepartmentAssets(
        slug=dept.slug,
        code=dept.code,
        label=dept.label,
        metrics=DepartmentMetrics(
            skill_count=len(skills),
            agent_count=len(agents),
            tool_count=len(tools),
        ),
        skills=skills,
        agents=agents,
        tools=tools,
    )


def _bucket_of(category_id: str | None) -> str:
    """Which page a row belongs to: its own department, or the unassigned one."""
    if category_id and category_id in DEPARTMENT_CATEGORY_IDS:
        return category_id
    return UNASSIGNED.slug


@router.get("", response_model=list[DepartmentSummary])
def list_departments(
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> list[DepartmentSummary]:
    """The nine departments plus the unassigned bucket, each with its counts.

    Two grouped COUNT queries rather than the per-department read that
    ``/{slug}/assets`` does: the sidebar asks for this on every page load, and
    ten departments × two kinds of row would be twenty round trips to render a
    navigation menu.

    The unassigned entry is last and always present, even at zero: the number it
    carries is the answer to "why does this page's total not match the market's",
    and a bucket that disappears when empty cannot answer that.
    """
    requester = acting_eid(request, x_user_eid)

    skills = _only_visible(
        select(RpAiSkill.category_id, func.count(RpAiSkill.id)).group_by(RpAiSkill.category_id),
        RpAiSkill,
        RpAiSkill.uploader_eid,
        requester,
    )
    items = _only_visible(
        select(
            RpAiMarketItem.category_id,
            RpAiMarketItem.item_type,
            func.count(RpAiMarketItem.id),
        )
        .where(RpAiMarketItem.item_type.in_(_ITEM_KINDS))
        .group_by(RpAiMarketItem.category_id, RpAiMarketItem.item_type),
        RpAiMarketItem,
        RpAiMarketItem.creator,
        requester,
    )

    tally: dict[str, dict[str, int]] = {
        d.slug: {"skill": 0, "agent": 0, "tool": 0} for d in (*DEPARTMENTS, UNASSIGNED)
    }
    for category_id, count in db.execute(skills).all():
        tally[_bucket_of(category_id)]["skill"] += int(count)
    for category_id, kind, count in db.execute(items).all():
        tally[_bucket_of(category_id)][kind] += int(count)

    return [
        DepartmentSummary(
            slug=dept.slug,
            code=dept.code,
            label=dept.label,
            metrics=DepartmentMetrics(
                skill_count=tally[dept.slug]["skill"],
                agent_count=tally[dept.slug]["agent"],
                tool_count=tally[dept.slug]["tool"],
            ),
        )
        for dept in (*DEPARTMENTS, UNASSIGNED)
    ]


@router.get("/{slug}/assets", response_model=DepartmentAssets)
def department_assets(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> DepartmentAssets:
    """One department's headline numbers and the three lists behind them."""
    dept = department_by_slug(slug)
    if dept is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"未知部门 '{slug}'。")
    return _assets(db, dept, acting_eid(request, x_user_eid))
