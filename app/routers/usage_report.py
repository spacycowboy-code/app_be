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

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
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
        # and the fix is to refresh the snapshot rather than to look at a traceback.
        logger.warning("Usage report unavailable, no org snapshot at %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "组织结构快照不存在，报告里的部门人数无从计算。"
            f"请用 tools/fetch_graph_tree.py 刷新到 {exc}，"
            "或把 USAGE_REPORT_ORG_SNAPSHOT 指到已有的快照。",
        ) from exc
    # No caching: the point of generating this per request is that the numbers are
    # current, and a stale copy in a proxy would undo that silently.
    return HTMLResponse(
        page, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
    )
