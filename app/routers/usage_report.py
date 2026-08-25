"""The department usage report, rendered on request as one HTML page.

Served at ``/api/department-report``. Not ``/api/usage-report`` — that name was
already taken by the weekly figures in ``routers/usage.py``, and FastAPI would have
matched whichever was registered first, leaving one of the two silently
unreachable.

Restricted to ``dashboard_eids``. The page names individuals and shows what each
of them uploaded and downloaded, so the same list that guards the sign-in
statistics guards this. The sidebar also hides the link, but hiding a link is not
access control and this endpoint does not rely on it.

Returned as HTML rather than JSON because the report is the deliverable: it is
read by people, printed, and mailed on.
"""
from __future__ import annotations

import json
import logging

from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.org_snapshot import RpAiOrgSnapshot
from app.services.usage_report import OrgSnapshotMissing, build_html

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["usage-report"])


def require_dashboard_eid(request: Request) -> str:
    """Allow only the EIDs configured for the Dashboard.

    The site-wide gate in ``app.main`` has already rejected anonymous callers, so
    reaching here means a valid session — just not necessarily an allowed one.
    """
    eid = str(getattr(request.state, "eid", "") or "").strip().upper()
    if not eid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    if eid not in get_settings().dashboard_eid_list:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You are not allowed to view the usage report.",
        )
    return eid


@router.get(
    "/department-report",
    response_class=HTMLResponse,
    summary="Department usage report (HTML page, Dashboard EIDs only)",
)
def usage_report(
    db: Session = Depends(get_db),
    _eid: str = Depends(require_dashboard_eid),
) -> HTMLResponse:
    settings = get_settings()
    try:
        page = build_html(
            db.connection(),
            settings.usage_report_org_snapshot,
            settings.usage_report_identity_snapshot or None,
            settings.ldap_bind_user,
        )
    except OrgSnapshotMissing as exc:
        # 503 rather than 500: the service is fine, an input it needs is not here,
        # and the fix is to push a snapshot rather than to look at a traceback.
        logger.warning("Usage report unavailable, no org snapshot (file: %s)", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "组织结构快照还没上传，报告里的部门人数无从计算。"
            "在能拿到 Graph token 的机器上跑："
            "python tools/fetch_graph_tree.py --push <门户地址> --portal-token <你的登录 token>。"
            "本地开发也可以把 USAGE_REPORT_ORG_SNAPSHOT 指到已有的快照文件。",
        ) from exc
    # No caching: the point of generating this per request is that the numbers are
    # current, and a stale copy in a proxy would undo that silently.
    return HTMLResponse(
        page, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
    )


@router.put(
    "/department-report/org-snapshot",
    status_code=status.HTTP_200_OK,
    summary="Upload the Graph reporting-tree snapshot (Dashboard EIDs only)",
)
def put_org_snapshot(
    payload: dict = Body(..., description="The Graph walk, keyed by object id"),
    db: Session = Depends(get_db),
    eid: str = Depends(require_dashboard_eid),
) -> dict:
    """Replace the org structure the report counts people by.

    Uploaded rather than fetched by the service: walking 945 people through Graph
    takes minutes and needs a token this service does not hold. It is pushed from
    wherever that token can be obtained, and kept in the database so a redeploy
    does not lose it.

    Validated before it is stored. A snapshot that parses but is missing the
    reporting line would still render a report — one with plausible-looking head
    counts that are wrong, which is worse than no report.
    """
    from app.services.usage_report import org_tree as org_tree_mod

    try:
        tree = org_tree_mod.build(payload, datetime.now())
    except Exception as exc:  # noqa: BLE001 - the payload is external input
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"这份快照解析不了：{exc}",
        ) from exc

    people = tree.get("people") or {}
    if not people:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "这份快照里没有人。期望的是 Graph 的对象 id 到用户记录的映射，"
            "每条带 onPremisesSamAccountName、department、branch 和 manager_id。",
        )
    placed = sum(1 for rec in people.values() if rec.get("branch"))
    if not placed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"这份快照里 {len(people)} 个人都没有 branch，无法归到任何二级部门。"
            "抓取时应该记下汇报树走到的分支（见 tools/fetch_graph_tree.py）。",
        )

    # Replaced, not appended: there is one current organisation, and a report that
    # silently used an older row would be impossible to explain.
    db.query(RpAiOrgSnapshot).delete()
    db.add(
        RpAiOrgSnapshot(
            payload=json.dumps(payload, ensure_ascii=False),
            captured_at=datetime.now(),
            uploaded_by=eid,
            people=len(people),
        )
    )
    db.commit()
    logger.info("Org snapshot replaced by %s: %d people", eid, len(people))
    # Echoed back so whoever pushed it can see the walk landed whole, rather than
    # finding out from the report that it was short a few hundred people.
    return {
        "people": len(people),
        "placed": placed,
        "departments": sorted(
            {str(rec["branch"]) for rec in people.values() if rec.get("branch")}
        ),
    }
