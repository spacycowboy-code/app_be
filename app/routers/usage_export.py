"""Admin-only raw usage export: one JSON file, one row per recorded action.

Answers three questions the Dashboard's existing panels deliberately do not:

* for every skill and every market item (tool / agent) — **who** downloaded it and
  **when**, and who favourited it and when;
* for every tool — **how many clicks**, by whom, at what time;
* which assets have no activity at all.

**Why a separate endpoint rather than widening ``/api/usage-report``.** That report
is aggregate on purpose and a test pins the absence of EIDs in its payload
(``tests/test_usage_events.py::test_the_report_names_no_individuals``): a
per-person ranking published weekly would make sign-ins and tool runs worth
gaming. This export is the opposite thing — named individuals, no ranking, pulled
by hand by one of the Dashboard EIDs — so it lives on its own route with its own
gate instead of loosening the report.

**Nothing is filtered out.** Two kinds of row are easy to drop by accident and are
kept here on purpose, because an export is the one place where a missing row
cannot be noticed:

* anonymous downloads (``rp_ai_skill_download.eid`` is nullable) are emitted with
  ``eid: null`` and also counted separately, rather than skipped;
* usage events whose market item has since been deleted are kept — they carry no
  foreign key by design — and flagged with ``objectStillListed: false`` so the
  reader can exclude them deliberately. ``/api/usage-report`` excludes them; this
  file lets you see both answers.

**What is included from the RVC/IVC push.** Downloads and favourites that happened
on an upstream system are pushed to ``POST /api/integrations/ivc/usage/events``,
which writes them into these same portal tables after logging them in
``rp_ai_external_usage_event`` — so they are in this file, counted like any other.
The one thing only that ledger has is *unlike* history: ``rp_ai_user_favorite`` is
state, not a log, so a favourite that was later removed leaves nothing here to
export.

**Where each department comes from.** Download and usage-event rows carry a
*snapshot* of the actor's department as it was at the time, and that snapshot is
what is emitted (see ``models/usage_event.py`` for why). The favourite tables
carry no snapshot, so those rows show the person's department **as it is today**;
the field is named ``departmentNow`` there so the two cannot be read as the same
measurement.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.download import RpAiSkillDownload
from app.models.favorite import RpAiUserFavorite
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.market_favorite import RpAiMarketFavorite
from app.models.skill import RpAiSkill
from app.models.usage_event import (
    OBJECT_MARKET_ITEM,
    VERB_INSTALL_COPIED,
    VERB_TOOL_OPEN,
    VERB_TOOL_RUN,
    RpAiUsageEvent,
)
from app.models.user import RpAiPortal
from app.schemas.common import as_utc

# The same gate as the weekly report, imported rather than re-implemented: a
# second copy of "who may see named individuals" is a copy that can drift.
from app.routers.usage import _require_dashboard
from app.services import reporting_windows

router = APIRouter(prefix="/api/usage-export", tags=["usage"])


def _iso(value: datetime | None) -> str | None:
    """A naive DB timestamp as a labelled UTC ISO string (see schemas.common)."""
    if value is None:
        return None
    return str(as_utc(value))


def _people(db: Session) -> dict[str, RpAiPortal]:
    """EID -> user row, for display names. One query; missing rows are normal."""
    return {u.eid: u for u in db.scalars(select(RpAiPortal)).all()}


def _actor(people: dict[str, RpAiPortal], eid: str | None) -> dict[str, Any]:
    """The person fields shared by every row: who, and their name if known."""
    user = people.get((eid or "").upper()) if eid else None
    return {
        "eid": eid,
        "displayName": user.display_name if user else None,
    }


@router.get("")
def usage_export(
    request: Request,
    db: Session = Depends(get_db),
    days: int | None = Query(
        None,
        ge=1,
        le=3650,
        description="Only include events from the last N days. Omit for all history. "
        "Assets are always listed in full, so an asset with no activity in the "
        "window still appears with empty lists.",
    ),
    _: str = Depends(_require_dashboard),
) -> JSONResponse:
    """Every download, favourite and tool action as one downloadable JSON file.

    Served as an attachment with a dated filename: the figures move (a deleted
    asset takes its download history with it), so two exports taken a week apart
    are different documents and must not be able to overwrite each other.
    """
    since = reporting_windows.db_now() - timedelta(days=days) if days else None
    people = _people(db)

    def in_window(column):  # noqa: ANN001,ANN202 - SQLAlchemy expression
        return [] if since is None else [column >= since]

    # --- Events, grouped by the asset they belong to -------------------------

    skill_downloads: dict[str, list[dict[str, Any]]] = {}
    for row in db.execute(
        select(
            RpAiSkillDownload.skill_id,
            RpAiSkillDownload.eid,
            RpAiSkillDownload.department,
            RpAiSkillDownload.created_at,
        )
        .where(*in_window(RpAiSkillDownload.created_at))
        .order_by(RpAiSkillDownload.created_at)
    ).all():
        skill_downloads.setdefault(row.skill_id, []).append(
            {
                **_actor(people, row.eid),
                # The department as it was at the time, not today's.
                "department": row.department,
                "at": _iso(row.created_at),
            }
        )

    item_downloads: dict[str, list[dict[str, Any]]] = {}
    for row in db.execute(
        select(
            RpAiMarketDownload.item_id,
            RpAiMarketDownload.eid,
            RpAiMarketDownload.department,
            RpAiMarketDownload.created_at,
        )
        .where(*in_window(RpAiMarketDownload.created_at))
        .order_by(RpAiMarketDownload.created_at)
    ).all():
        item_downloads.setdefault(row.item_id, []).append(
            {
                **_actor(people, row.eid),
                "department": row.department,
                "at": _iso(row.created_at),
            }
        )

    def favorite_rows(model, key_column):  # noqa: ANN001,ANN202
        """Favourites grouped by asset. ``departmentNow`` — no snapshot exists."""
        out: dict[str, list[dict[str, Any]]] = {}
        for row in db.execute(
            select(key_column, model.eid, model.created_at)
            .where(*in_window(model.created_at))
            .order_by(model.created_at)
        ).all():
            user = people.get((row.eid or "").upper())
            out.setdefault(row[0], []).append(
                {
                    **_actor(people, row.eid),
                    "departmentNow": user.department if user else None,
                    "at": _iso(row.created_at),
                }
            )
        return out

    skill_favorites = favorite_rows(RpAiUserFavorite, RpAiUserFavorite.skill_id)
    item_favorites = favorite_rows(RpAiMarketFavorite, RpAiMarketFavorite.item_id)

    # --- Assets --------------------------------------------------------------

    def asset(
        *,
        asset_id: str,
        name: str,
        asset_kind: str,
        version: str | None,
        status: str | None,
        category_id: str | None,
        creator_eid: str | None,
        downloads: list[dict[str, Any]],
        favorites: list[dict[str, Any]],
    ) -> dict[str, Any]:
        named = [d for d in downloads if d["eid"]]
        return {
            "id": asset_id,
            "name": name,
            # "skill" | "tool" | "agent" | "ai-platform".
            "assetKind": asset_kind,
            "version": version,
            "status": status,
            "categoryId": category_id,
            "creatorEid": creator_eid,
            # Distinct EIDs — the same number the market card shows.
            "uniqueDownloaders": len({str(d["eid"]).upper() for d in named}),
            "downloadEvents": len(downloads),
            # Counted, not dropped: the download endpoint allows anonymous calls,
            # so these rows are real downloads with no person attached.
            "anonymousDownloads": len(downloads) - len(named),
            "favorites": len(favorites),
            "downloads": downloads,
            "favoritedBy": favorites,
        }

    skills = [
        asset(
            asset_id=s.id,
            name=s.name,
            asset_kind="skill",
            version=s.version,
            status=s.status,
            category_id=s.category_id,
            creator_eid=s.uploader_eid,
            downloads=skill_downloads.get(s.id, []),
            favorites=skill_favorites.get(s.id, []),
        )
        for s in db.scalars(select(RpAiSkill).order_by(RpAiSkill.id)).all()
    ]

    items = [
        asset(
            asset_id=i.id,
            name=i.name,
            asset_kind=i.item_type,
            version=i.version,
            status=i.status,
            category_id=i.category_id,
            creator_eid=i.creator,
            downloads=item_downloads.get(i.id, []),
            favorites=item_favorites.get(i.id, []),
        )
        for i in db.scalars(select(RpAiMarketItem).order_by(RpAiMarketItem.id)).all()
    ]

    # --- Tools --------------------------------------------------------------
    # One entry per thing acted on, with every action under it. ``opens`` is the
    # click count the UI produces (a card or a tool tile was clicked and the user
    # was sent to it); ``runs`` is a tool that actually executed and reported how
    # it ended. They are never summed — see models/usage_event.py.

    listed_items = {i["id"] for i in items}
    tools: dict[tuple[str, str], dict[str, Any]] = {}
    for ev in db.scalars(
        select(RpAiUsageEvent)
        .where(*in_window(RpAiUsageEvent.created_at))
        .order_by(RpAiUsageEvent.created_at)
    ).all():
        key = (ev.object_type, ev.object_id)
        entry = tools.get(key)
        if entry is None:
            entry = tools[key] = {
                "objectType": ev.object_type,
                "objectId": ev.object_id,
                "name": ev.object_name,
                # False only for a market item that has since been deleted. The
                # weekly report drops these; here they are kept and flagged.
                "objectStillListed": (
                    ev.object_id in listed_items
                    if ev.object_type == OBJECT_MARKET_ITEM
                    else True
                ),
                "opens": 0,
                "runs": 0,
                "failedRuns": 0,
                "installCommandsCopied": 0,
                "uniqueUsers": 0,
                "events": [],
            }
        # The name as last seen: it can change, and the newest one is the one a
        # reader will recognise.
        if ev.object_name:
            entry["name"] = ev.object_name
        if ev.verb == VERB_TOOL_OPEN:
            entry["opens"] += 1
        elif ev.verb == VERB_TOOL_RUN:
            entry["runs"] += 1
            if ev.outcome == "error":
                entry["failedRuns"] += 1
        elif ev.verb == VERB_INSTALL_COPIED:
            entry["installCommandsCopied"] += 1
        entry["events"].append(
            {
                **_actor(people, ev.eid),
                "department": ev.department,
                "verb": ev.verb,
                "source": ev.source,
                "outcome": ev.outcome,
                "durationMs": ev.duration_ms,
                "at": _iso(ev.created_at),
            }
        )

    tool_list = sorted(tools.values(), key=lambda t: (-t["opens"], -t["runs"], t["objectId"]))
    for entry in tool_list:
        entry["uniqueUsers"] = len({str(e["eid"]).upper() for e in entry["events"] if e["eid"]})

    now = reporting_windows.db_now()
    payload = {
        "generatedAt": _iso(now),
        "windowDays": days,
        "since": _iso(since),
        "skills": skills,
        "marketItems": items,
        "tools": tool_list,
        "totals": {
            "skills": len(skills),
            "marketItems": len(items),
            "skillDownloadEvents": sum(s["downloadEvents"] for s in skills),
            "marketDownloadEvents": sum(i["downloadEvents"] for i in items),
            "skillFavorites": sum(s["favorites"] for s in skills),
            "marketFavorites": sum(i["favorites"] for i in items),
            "toolOpens": sum(t["opens"] for t in tool_list),
            "toolRuns": sum(t["runs"] for t in tool_list),
            "installCommandsCopied": sum(t["installCommandsCopied"] for t in tool_list),
        },
    }

    stamp = now.strftime("%Y-%m-%d_%H%M")
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": f'attachment; filename="usage-export-{stamp}.json"'
        },
    )
