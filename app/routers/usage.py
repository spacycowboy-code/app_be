"""Usage recording, a person's own history, and the weekly report.

Three endpoints for the individual and one for the team, kept in a single file
because they share an authorisation rule that is easy to get wrong.

**The rule.** ``/api/users/{eid}/downloads`` and ``.../tools`` are readable by
that person and by the Dashboard EIDs — nobody else. This is deliberately
stricter than the neighbouring ``/api/users/{eid}/favorites`` and
``.../activities``, which any signed-in user can read for anyone. Favourites are
close to public: they are shown on the item cards. What someone downloaded and
which tools they open is a record of how a colleague works, and there is no
feature that needs one person to read another's. The looser endpoints are left
as they are rather than tightened in passing, because changing them would break
callers and belongs in its own change.

**Why the report has no per-person numbers.** See ``schemas.usage.UsageReport``.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.audit import RpAiSkillAuditRecord
from app.models.download import RpAiSkillDownload
from app.models.login_event import RpAiLoginEvent
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.services.download_counts import (
    MARKET,
    SKILL,
    unique_downloads,
    unique_downloads_by_department,
)
from app.models.skill import RpAiSkill
from app.models.usage_event import (
    OBJECT_MARKET_ITEM,
    VERB_INSTALL_COPIED,
    VERB_TOOL_OPEN,
    VERB_TOOL_RUN,
    RpAiUsageEvent,
)
from app.schemas.usage import (
    DownloadHistoryEntry,
    LabelCount,
    ObjectCount,
    ToolActivity,
    ToolUsage,
    UsageEventCreate,
    UsageReport,
)
from app.services import reporting_windows
from app.services.dept_groups import resolve_group
from app.services.eids import normalize_eid
from app.services.usage import department_of

router = APIRouter(prefix="/api", tags=["usage"])

#: How far back "frequently used tools" looks by default. A month is long enough
#: that a tool used weekly still appears, and short enough that the list follows
#: what someone is working on now rather than what they did last quarter.
DEFAULT_TOOL_WINDOW_DAYS = 30

#: Buckets shown in a top-N list. Kept small: a weekly write-up that lists
#: twenty items is not read.
TOP_N = 5


# --- Identity and authorisation ---------------------------------------------


def _caller_eid(request: Request) -> str:
    """The signed-in caller's EID, uppercase.

    When the site-wide gate is off (local development only — production runs
    with ``auth_enabled``), fall back to an explicit ``X-User-Eid`` header so the
    feature can be exercised without a token. The fallback is conditioned on the
    gate being off rather than applied whenever the token is missing, which is
    the difference between a development convenience and a way to attribute
    activity to a colleague in production.
    """
    eid = normalize_eid(str(getattr(request.state, "eid", "") or ""))
    if eid:
        return eid
    if not get_settings().auth_enabled:
        return normalize_eid(request.headers.get("X-User-Eid", "")) or ""
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")


def _require_self_or_dashboard(request: Request, eid: str) -> str:
    """Allow the subject themselves, or a Dashboard EID. 403 for anyone else."""
    caller = _caller_eid(request)
    subject = normalize_eid(eid) or ""
    if not caller:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    if caller != subject and caller not in get_settings().dashboard_eid_list:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You can only view your own usage history."
        )
    return subject


def _require_dashboard(request: Request) -> str:
    caller = _caller_eid(request)
    if not caller:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    if caller not in get_settings().dashboard_eid_list:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You are not allowed to view the usage report."
        )
    return caller


# --- Recording --------------------------------------------------------------


@router.post("/usage/events", status_code=status.HTTP_204_NO_CONTENT)
def record_usage_event(
    payload: UsageEventCreate, request: Request, db: Session = Depends(get_db)
) -> Response:
    """Record one action. The actor is the session, never the request body.

    A malformed call is a 4xx, because that is a bug in a caller and silence
    would hide it. A *storage* failure is swallowed: the user already performed
    the action being described, and failing here would turn a bookkeeping
    problem into a visible error for something that did work.
    """
    eid = _caller_eid(request)
    if not eid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")

    has_outcome = payload.outcome is not None or payload.duration_ms is not None
    if payload.verb == VERB_TOOL_RUN and payload.outcome is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "tool_run must report an outcome; otherwise a success rate cannot be computed.",
        )
    if payload.verb != VERB_TOOL_RUN and has_outcome:
        # Letting an open carry an outcome would quietly put un-runnable events
        # into the success-rate denominator.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{payload.verb} cannot carry an outcome or duration.",
        )

    try:
        db.add(
            RpAiUsageEvent(
                eid=eid,
                department=department_of(db, eid),
                verb=payload.verb,
                object_type=payload.object_type,
                object_id=payload.object_id,
                object_name=payload.object_name,
                source=payload.source,
                outcome=payload.outcome,
                duration_ms=payload.duration_ms,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- A person's own history -------------------------------------------------


@router.get("/users/{eid}/downloads", response_model=list[DownloadHistoryEntry])
def list_downloads(
    eid: str,
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
) -> list[DownloadHistoryEntry]:
    """What this person downloaded, newest first, skills and market items merged.

    Two queries rather than a UNION: the two tables reference different parents,
    so a union would need matching column lists and would still be sorted in
    Python. Each side is limited before merging, so the cost stays bounded.
    """
    subject = _require_self_or_dashboard(request, eid)

    skills = db.execute(
        select(
            RpAiSkillDownload.skill_id,
            RpAiSkillDownload.created_at,
            RpAiSkill.name,
            RpAiSkill.version,
        )
        .join(RpAiSkill, RpAiSkill.id == RpAiSkillDownload.skill_id)
        .where(RpAiSkillDownload.eid == subject)
        .order_by(RpAiSkillDownload.created_at.desc())
        .limit(limit)
    ).all()

    items = db.execute(
        select(
            RpAiMarketDownload.item_id,
            RpAiMarketDownload.created_at,
            RpAiMarketItem.name,
            RpAiMarketItem.version,
        )
        .join(RpAiMarketItem, RpAiMarketItem.id == RpAiMarketDownload.item_id)
        .where(RpAiMarketDownload.eid == subject)
        .order_by(RpAiMarketDownload.created_at.desc())
        .limit(limit)
    ).all()

    merged = [
        DownloadHistoryEntry(
            object_type="skill",
            object_id=row[0],
            name=row[2],
            version=row[3],
            downloaded_at=row[1],
        )
        for row in skills
    ] + [
        DownloadHistoryEntry(
            object_type=OBJECT_MARKET_ITEM,
            object_id=row[0],
            name=row[2],
            version=row[3],
            downloaded_at=row[1],
        )
        for row in items
    ]
    merged.sort(key=lambda e: e.downloaded_at, reverse=True)
    return merged[:limit]


@router.get("/users/{eid}/tools", response_model=list[ToolUsage])
def list_tools(
    eid: str,
    request: Request,
    db: Session = Depends(get_db),
    days: int = Query(DEFAULT_TOOL_WINDOW_DAYS, ge=1, le=365),
) -> list[ToolUsage]:
    """This person's tools over the last ``days``, busiest first.

    Runs and opens are counted separately and never summed — see
    ``schemas.usage.ToolUsage``. Ordering uses runs first, then opens, so a tool
    actually executed outranks one merely opened the same number of times.
    """
    subject = _require_self_or_dashboard(request, eid)
    since = reporting_windows.db_now() - timedelta(days=days)

    rows = db.execute(
        select(
            RpAiUsageEvent.object_type,
            RpAiUsageEvent.object_id,
            # The name as last seen. Names change rarely; taking the maximum
            # avoids a second query per tool and the worst case is showing a
            # previous label, not a wrong count.
            func.max(RpAiUsageEvent.object_name).label("name"),
            func.sum(_case_when(RpAiUsageEvent.verb == VERB_TOOL_RUN)).label("runs"),
            func.sum(_case_when(RpAiUsageEvent.verb == VERB_TOOL_OPEN)).label("opens"),
            func.sum(
                _case_when(
                    (RpAiUsageEvent.verb == VERB_TOOL_RUN)
                    & (RpAiUsageEvent.outcome == "error")
                )
            ).label("failed"),
            func.max(RpAiUsageEvent.created_at).label("last_used_at"),
        )
        .where(
            RpAiUsageEvent.eid == subject,
            RpAiUsageEvent.created_at >= since,
            RpAiUsageEvent.verb.in_([VERB_TOOL_RUN, VERB_TOOL_OPEN]),
            _object_still_listed(),
        )
        .group_by(RpAiUsageEvent.object_type, RpAiUsageEvent.object_id)
    ).all()

    out = [
        ToolUsage(
            object_type=row.object_type,
            object_id=row.object_id,
            name=row.name,
            runs=int(row.runs or 0),
            opens=int(row.opens or 0),
            failed_runs=int(row.failed or 0),
            last_used_at=row.last_used_at,
        )
        for row in rows
    ]
    out.sort(key=lambda t: (t.runs, t.opens, t.last_used_at), reverse=True)
    return out


def _object_still_listed():  # noqa: ANN201 - SQLAlchemy expression
    """True when the thing a usage event describes is still on the market.

    Usage events outlive their subject on purpose: ``rp_ai_usage_event`` carries no
    foreign key to ``rp_ai_market_item``, and stores ``object_name`` as it was, so a
    report can still name something that has since gone. The side effect is that a
    deleted agent keeps accumulating opens and runs in every count derived from this
    table, which is what this predicate stops.

    Only ``market_item`` rows are checked. ``object_type = 'tool'`` names a built-in
    portal tool page, which has no row in ``rp_ai_market_item`` and never had one —
    requiring existence for those would drop every event for the portal's own pages.
    """
    return (RpAiUsageEvent.object_type != OBJECT_MARKET_ITEM) | select(
        RpAiMarketItem.id
    ).where(
        RpAiMarketItem.id == RpAiUsageEvent.object_id,
        RpAiMarketItem.status == "approved",
    ).exists()


def _case_when(condition):  # noqa: ANN001,ANN201 - SQLAlchemy expression
    """1 when ``condition`` holds, else 0 — for counting inside one GROUP BY.

    Written once because the alternative is a separate query per counter, and
    those would each need the same window and filter repeated.
    """
    return case((condition, 1), else_=0)


# --- The weekly report ------------------------------------------------------


@router.get("/usage-report", response_model=UsageReport)
def usage_report(
    request: Request,
    db: Session = Depends(get_db),
    weeks_back: int = Query(
        1,
        ge=0,
        le=52,
        # Aliased because the rest of this API speaks camelCase to the browser
        # (see schemas.common.CamelModel); a snake_case query parameter would be
        # silently ignored and the caller would get the default instead.
        alias="weeksBack",
        description="0 = this week so far; 1 = last full week (the default, "
        "because a report is written about a week that has ended).",
    ),
    _: str = Depends(_require_dashboard),
) -> UsageReport:
    """Aggregates for a weekly write-up. Counts of things and of distinct people.

    Defaults to the last *complete* week: a report written on Monday about "this
    week" would show almost nothing and read as a collapse in usage.
    """
    window = reporting_windows.current_week(weeks_back=weeks_back)
    since, until = window.since, window.until

    # --- Adoption ---
    active_people = (
        db.scalar(
            select(func.count(func.distinct(RpAiLoginEvent.eid))).where(
                RpAiLoginEvent.created_at >= since, RpAiLoginEvent.created_at < until
            )
        )
        or 0
    )

    # First-ever sign-in inside the window. Computed from the log rather than a
    # "created" column so it stays true for users imported before the log began.
    first_seen = (
        select(
            RpAiLoginEvent.eid.label("eid"),
            func.min(RpAiLoginEvent.created_at).label("first_at"),
        )
        .group_by(RpAiLoginEvent.eid)
        .subquery()
    )
    new_people = (
        db.scalar(
            select(func.count()).select_from(first_seen).where(
                first_seen.c.first_at >= since, first_seen.c.first_at < until
            )
        )
        or 0
    )

    prev_since = since - (until - since)
    previous_week_people = set(
        db.scalars(
            select(func.distinct(RpAiLoginEvent.eid)).where(
                RpAiLoginEvent.created_at >= prev_since, RpAiLoginEvent.created_at < since
            )
        ).all()
    )
    this_week_people = set(
        db.scalars(
            select(func.distinct(RpAiLoginEvent.eid)).where(
                RpAiLoginEvent.created_at >= since, RpAiLoginEvent.created_at < until
            )
        ).all()
    )
    returning_people = len(this_week_people & previous_week_people)

    # --- Group activity ---
    # Departments are read from the event rows' own snapshots, so a
    # reorganisation cannot rewrite a past week (see services/usage.py). Actions
    # counted here are downloads plus tool/install events — everything that
    # carries a snapshot. Sign-ins are not included: rp_ai_login_event stores an
    # EID and a timestamp only, by design, so attributing one to a group would
    # mean joining today's department onto a past sign-in.
    group_counts: dict[str, int] = {}
    # The third table needs one extra condition the other two do not: usage events
    # survive the deletion of the market item they describe, so counting them raw
    # would credit a group for opening something that is no longer on the market.
    # 两张下载表按 (条目, 人) 去重后再归部门（services/download_counts 一处规则）。
    # ``RpAiUsageEvent`` 不去重：它记的是"打开/安装了一个工具"这类使用动作,同一个人每天
    # 用一次就是活跃一次,把它折成 1 会把"一直在用"变成"用过一次"。两者口径不同是有意的。
    for kind in (SKILL, MARKET):
        for dept, count in unique_downloads_by_department(
            db, kind, since=since, until=until
        ).items():
            label = resolve_group(dept) or "unmapped"
            group_counts[label] = group_counts.get(label, 0) + int(count or 0)
    for dept, count in db.execute(
        select(RpAiUsageEvent.department, func.count())
        .where(
            RpAiUsageEvent.created_at >= since,
            RpAiUsageEvent.created_at < until,
            _object_still_listed(),
        )
        .group_by(RpAiUsageEvent.department)
    ).all():
        label = resolve_group(dept) or "unmapped"
        group_counts[label] = group_counts.get(label, 0) + int(count or 0)
    by_group = sorted(
        (LabelCount(label=k, count=v) for k, v in group_counts.items()),
        key=lambda c: c.count,
        reverse=True,
    )
    active_groups = len([k for k in group_counts if k != "unmapped"])

    # --- Assets added ---
    # Both counts are narrowed to what is on the market now. A skill taken down is
    # ``archived``, not removed, so an unfiltered count would keep reporting it as an
    # asset the reader cannot find in the portal; ``pending`` and ``rejected`` were
    # never visible to anybody.
    skills_added = (
        db.scalar(
            select(func.count(RpAiSkill.id)).where(
                RpAiSkill.created_at >= since,
                RpAiSkill.created_at < until,
                RpAiSkill.status == "approved",
            )
        )
        or 0
    )
    items_added = (
        db.scalar(
            select(func.count(RpAiMarketItem.id)).where(
                RpAiMarketItem.created_at >= since,
                RpAiMarketItem.created_at < until,
                RpAiMarketItem.status == "approved",
            )
        )
        or 0
    )

    # --- Downloads ---
    # 去重后的每条目下载人数（services/download_counts 一处规则）。原本是 func.count()
    # 数行数,于是线上一个跑了 93 次的脚本把 outlook-assistant 顶到榜首 —— 而这张榜是拿来
    # 做决策的。去重是**在窗口内**做的:一个人这个月拿过就算一次,下个月再拿再算一次。
    skill_names = dict(db.execute(select(RpAiSkill.id, RpAiSkill.name)).all())
    item_names = dict(db.execute(select(RpAiMarketItem.id, RpAiMarketItem.name)).all())
    skill_dl = [
        (oid, skill_names.get(oid, oid), n)
        for oid, n in unique_downloads(db, SKILL, since=since, until=until).items()
        if oid in skill_names
    ]
    item_dl = [
        (oid, item_names.get(oid, oid), n)
        for oid, n in unique_downloads(db, MARKET, since=since, until=until).items()
        if oid in item_names
    ]

    downloads = sum(r[2] for r in skill_dl) + sum(r[2] for r in item_dl)
    top_downloads = sorted(
        (
            ObjectCount(object_id=oid, name=name, count=int(n))
            for oid, name, n in skill_dl + item_dl
        ),
        key=lambda c: c.count,
        reverse=True,
    )[:TOP_N]

    distinct_downloaders = len(
        set(
            db.scalars(
                select(func.distinct(RpAiSkillDownload.eid)).where(
                    RpAiSkillDownload.eid.is_not(None),
                    RpAiSkillDownload.created_at >= since,
                    RpAiSkillDownload.created_at < until,
                )
            ).all()
        )
        | set(
            db.scalars(
                select(func.distinct(RpAiMarketDownload.eid)).where(
                    RpAiMarketDownload.eid.is_not(None),
                    RpAiMarketDownload.created_at >= since,
                    RpAiMarketDownload.created_at < until,
                )
            ).all()
        )
    )

    # Assets that existed before the window closed and were not downloaded in
    # it. The candidate list for retirement, which the top-N list cannot show.
    # Narrowed to what is still listed: a taken-down skill is not a retirement
    # candidate, it is already retired, and counting it here would inflate the very
    # number that is supposed to drive take-downs.
    total_assets = (
        db.scalar(
            select(func.count(RpAiSkill.id)).where(
                RpAiSkill.created_at < until, RpAiSkill.status == "approved"
            )
        )
        or 0
    ) + (
        db.scalar(
            select(func.count(RpAiMarketItem.id)).where(
                RpAiMarketItem.created_at < until,
                RpAiMarketItem.status == "approved",
            )
        )
        or 0
    )
    unused_assets = max(total_assets - (len(skill_dl) + len(item_dl)), 0)

    install_commands_copied = (
        db.scalar(
            select(func.count(RpAiUsageEvent.id)).where(
                RpAiUsageEvent.verb == VERB_INSTALL_COPIED,
                RpAiUsageEvent.created_at >= since,
                RpAiUsageEvent.created_at < until,
                _object_still_listed(),
            )
        )
        or 0
    )

    # --- Audit outcomes ---
    audits_total = (
        db.scalar(
            select(func.count(RpAiSkillAuditRecord.id)).where(
                RpAiSkillAuditRecord.created_at >= since,
                RpAiSkillAuditRecord.created_at < until,
            )
        )
        or 0
    )
    audits_blocked = (
        db.scalar(
            select(func.count(RpAiSkillAuditRecord.id)).where(
                RpAiSkillAuditRecord.state == "danger",
                RpAiSkillAuditRecord.created_at >= since,
                RpAiSkillAuditRecord.created_at < until,
            )
        )
        or 0
    )
    # ``reason`` is the short cause already shown to a rejected uploader, so the
    # report and the uploader see the same words. It is free text, so these are
    # buckets by wording rather than by a closed vocabulary.
    audit_reasons = [
        LabelCount(label=row[0] or "unspecified", count=int(row[1]))
        for row in db.execute(
            select(RpAiSkillAuditRecord.reason, func.count())
            .where(
                RpAiSkillAuditRecord.state == "danger",
                RpAiSkillAuditRecord.created_at >= since,
                RpAiSkillAuditRecord.created_at < until,
            )
            .group_by(RpAiSkillAuditRecord.reason)
            .order_by(func.count().desc())
            .limit(TOP_N)
        ).all()
    ]

    # --- Tools ---
    tool_rows = db.execute(
        select(
            RpAiUsageEvent.object_type,
            RpAiUsageEvent.object_id,
            func.max(RpAiUsageEvent.object_name).label("name"),
            func.sum(_case_when(RpAiUsageEvent.verb == VERB_TOOL_RUN)).label("runs"),
            func.sum(_case_when(RpAiUsageEvent.verb == VERB_TOOL_OPEN)).label("opens"),
            func.sum(
                _case_when(
                    (RpAiUsageEvent.verb == VERB_TOOL_RUN)
                    & (RpAiUsageEvent.outcome == "error")
                )
            ).label("failed"),
        )
        .where(
            RpAiUsageEvent.created_at >= since,
            RpAiUsageEvent.created_at < until,
            RpAiUsageEvent.verb.in_([VERB_TOOL_RUN, VERB_TOOL_OPEN]),
        )
        .group_by(RpAiUsageEvent.object_type, RpAiUsageEvent.object_id)
    ).all()
    tools = sorted(
        (
            ToolActivity(
                object_type=r.object_type,
                object_id=r.object_id,
                name=r.name,
                runs=int(r.runs or 0),
                failed_runs=int(r.failed or 0),
                opens=int(r.opens or 0),
            )
            for r in tool_rows
        ),
        key=lambda t: (t.runs, t.opens),
        reverse=True,
    )
    failed_runs = sum(t.failed_runs for t in tools)

    return UsageReport(
        since=window.since_local,
        until=window.until_local,
        complete=weeks_back > 0,
        active_people=int(active_people),
        new_people=int(new_people),
        active_groups=active_groups,
        by_group=by_group,
        skills_added=int(skills_added),
        items_added=int(items_added),
        top_downloads=top_downloads,
        unused_assets=unused_assets,
        downloads=downloads,
        distinct_downloaders=distinct_downloaders,
        install_commands_copied=int(install_commands_copied),
        returning_people=returning_people,
        audits_blocked=int(audits_blocked),
        audits_total=int(audits_total),
        audit_reasons=audit_reasons,
        failed_runs=failed_runs,
        tools=tools,
    )
