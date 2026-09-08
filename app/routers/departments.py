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
from sqlalchemy import Select, or_, select
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
from app.models.user import RpAiPortal
from app.services.departments import (
    DEPARTMENTS,
    UNASSIGNED,
    Department,
    department_by_slug,
    department_for_directory_value,
)
from app.services.eids import acting_eid, looks_like_signum, normalize_eid

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


def _slug_by_eid(db: Session) -> dict[str, str]:
    """Every portal user's department page, keyed by upper-case EID.

    One read of the user table per request instead of a join per query. The table
    is a few hundred rows and both the list endpoint and the per-department
    endpoint need the whole mapping anyway — the counts endpoint has to bucket
    every asset, and the assets endpoint needs "everyone in this department"
    before it can filter.

    Users whose directory value names no RP department are simply absent, so a
    lookup miss and an unplaceable department are one case at every call site.
    That is intentional: the page cannot act differently on "never signed in",
    "signed in but the directory gave no department", and "department outside
    RP", and pretending otherwise would put three labels on one bucket.
    """
    mapping: dict[str, str] = {}
    for eid, department in db.execute(select(RpAiPortal.eid, RpAiPortal.department)).all():
        dept = department_for_directory_value(department)
        normalized = normalize_eid(eid)
        if dept and normalized:
            mapping[normalized] = dept.slug
    return mapping


def _skill_owner(skill: RpAiSkill) -> str | None:
    """Whose organisation a skill counts towards."""
    return normalize_eid(skill.uploader_eid)


def _item_owner(item: RpAiMarketItem) -> str | None:
    """Whose organisation a market item counts towards.

    ``creator`` first, then ``author`` but only when it is shaped like a signum.
    The fallback exists because rows imported from GitLab carry no ``creator``
    while several do carry a real signum in ``author`` — three such rows in
    production resolve to a real person this way, and without the fallback they
    would sit on the Unassigned page while their author's name is printed on the
    card.

    A display name is refused rather than returned. Being precise about what that
    buys, because it is less than it looks: attribution is a lookup keyed by EID,
    and ``Jinhang Li`` cannot match an EID, so the page's output is the same
    either way. What the check protects is this function's contract — it promises
    an EID or ``None``, and a caller that took its result to mean "a person" would
    otherwise be handed a display name. ``tools/refile_market_categories.py``
    applies the same rule to decide whether it may refile a row, where it does
    change an outcome: the row is left alone and reported instead of moved.

    Pinned by a unit test on this function rather than only through the endpoint,
    since an endpoint test cannot tell the two behaviours apart.
    """
    if item.creator:
        return normalize_eid(item.creator)
    if looks_like_signum(item.author):
        return normalize_eid(item.author)
    return None


def _slug_of(owner_eid: str | None, slug_by_eid: dict[str, str]) -> str:
    """Which page a row belongs to: its owner's department, or the unassigned one."""
    if not owner_eid:
        return UNASSIGNED.slug
    return slug_by_eid.get(owner_eid, UNASSIGNED.slug)


def _skills_of(
    db: Session, dept: Department, requester: str | None, slug_by_eid: dict[str, str]
) -> list[RpAiSkill]:
    stmt = _only_visible(select(RpAiSkill), RpAiSkill, RpAiSkill.uploader_eid, requester)
    skills = [
        skill
        for skill in db.scalars(stmt.order_by(RpAiSkill.name)).all()
        if _slug_of(_skill_owner(skill), slug_by_eid) == dept.slug
    ]
    attach_skill_stats(db, skills)
    return skills


def _items_of(
    db: Session, dept: Department, requester: str | None, slug_by_eid: dict[str, str]
) -> dict[str, list[RpAiMarketItem]]:
    """The department's market items, bucketed by ``item_type``.

    One query for both kinds rather than one per kind: the counts and the lists
    have to come from the same read, or a concurrent publish can make the header
    disagree with the cards under it.

    The department filter is applied in Python rather than in SQL because
    attribution now runs through the owner's directory string, and the same
    function has to decide it here and in the counts endpoint — expressing it
    twice, once as a ``WHERE`` and once as a dict lookup, is how a page's header
    starts disagreeing with the list under it. The row counts here are in the
    dozens, so there is nothing to win by pushing it down.
    """
    stmt = _only_visible(select(RpAiMarketItem), RpAiMarketItem, RpAiMarketItem.creator, requester)
    stmt = stmt.where(RpAiMarketItem.item_type.in_(_ITEM_KINDS))
    items = [
        item
        for item in db.scalars(stmt.order_by(RpAiMarketItem.name)).all()
        if _slug_of(_item_owner(item), slug_by_eid) == dept.slug
    ]
    attach_market_stats(db, items)
    buckets: dict[str, list[RpAiMarketItem]] = {kind: [] for kind in _ITEM_KINDS}
    for item in items:
        buckets[item.item_type].append(item)
    return buckets


def _assets(db: Session, dept: Department, requester: str | None) -> DepartmentAssets:
    slug_by_eid = _slug_by_eid(db)
    skills = _skills_of(db, dept, requester, slug_by_eid)
    buckets = _items_of(db, dept, requester, slug_by_eid)
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


@router.get("", response_model=list[DepartmentSummary])
def list_departments(
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> list[DepartmentSummary]:
    """The nine departments plus the unassigned bucket, each with its counts.

    Two reads plus one pass over the rows, rather than the per-department query
    that ``/{slug}/assets`` does: the sidebar asks for this on every page load,
    and ten departments × two kinds of row would be twenty round trips to render
    a navigation menu.

    Grouped ``COUNT``s used to do it, which no longer works: attribution runs
    through each owner's directory string, so the grouping key is not a column.
    The rows are counted in Python through the same ``_slug_of`` the assets
    endpoint uses, which is the point — a second expression of the same rule is
    how a sidebar number starts disagreeing with the page it opens.

    The unassigned entry is last and always present, even at zero: the number it
    carries is the answer to "why does this page's total not match the market's",
    and a bucket that disappears when empty cannot answer that.
    """
    requester = acting_eid(request, x_user_eid)
    slug_by_eid = _slug_by_eid(db)

    skills = _only_visible(
        select(RpAiSkill.uploader_eid),
        RpAiSkill,
        RpAiSkill.uploader_eid,
        requester,
    )
    items = _only_visible(
        select(
            RpAiMarketItem.creator,
            RpAiMarketItem.author,
            RpAiMarketItem.item_type,
        ).where(RpAiMarketItem.item_type.in_(_ITEM_KINDS)),
        RpAiMarketItem,
        RpAiMarketItem.creator,
        requester,
    )

    tally: dict[str, dict[str, int]] = {
        d.slug: {"skill": 0, "agent": 0, "tool": 0} for d in (*DEPARTMENTS, UNASSIGNED)
    }
    for (uploader_eid,) in db.execute(skills).all():
        tally[_slug_of(normalize_eid(uploader_eid), slug_by_eid)]["skill"] += 1
    for creator, author, kind in db.execute(items).all():
        owner = normalize_eid(creator) if creator else (
            normalize_eid(author) if looks_like_signum(author) else None
        )
        tally[_slug_of(owner, slug_by_eid)][kind] += 1

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
