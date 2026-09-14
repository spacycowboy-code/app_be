"""R&D Guide endpoints.

GET  /api/rsw/rdg/stages          — full stage+node tree (all authenticated users)
GET  /api/rsw/rdg/tool-mappings   — node_key → [tool_name] map (all authenticated users)
GET  /api/rsw/rdg/admins          — who may edit the guide (rsw_admin only)
POST /api/rsw/rdg/admins          — add an editor (rsw_admin only)
DELETE /api/rsw/rdg/admins/{eid}  — remove an editor, never yourself (rsw_admin only)
POST /api/rsw/rdg/nodes           — create node (rsw_admin only)
PUT  /api/rsw/rdg/nodes/{id}      — update node (rsw_admin only)
DELETE /api/rsw/rdg/nodes/{id}    — delete node + its tool mappings (rsw_admin only)
PUT  /api/rsw/rdg/tool-mappings   — replace tool list for a node_key (rsw_admin only)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.rdg import RpAiRdgAdmin, RpAiRdgNode, RpAiRdgToolMapping

router = APIRouter(prefix="/api/rsw/rdg", tags=["rsw-rdg"])

# Fixed stage metadata — stage list itself is not stored in DB.
STAGES_META = [
    {"id": "early-phase", "label": "Early Phase",    "group": "exec"},
    {"id": "study",       "label": "Study",           "group": "exec"},
    {"id": "development", "label": "Development",     "group": "exec"},
    {"id": "test",        "label": "Test",            "group": "exec"},
    {"id": "ci",          "label": "CI",              "group": "exec"},
    {"id": "driver",      "label": "OPO & Driver",    "group": "support"},
    {"id": "debug",       "label": "Debug & Support", "group": "support"},
]


def _is_rsw_admin(eid: str, db: Session) -> bool:
    return db.scalar(select(RpAiRdgAdmin).where(RpAiRdgAdmin.eid == eid.upper())) is not None


def _require_rsw_admin(request: Request, db: Session) -> str:
    eid = str(getattr(request.state, "eid", "") or "").upper()
    if not eid or not _is_rsw_admin(eid, db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "RSW admin access required.")
    return eid


def _build_node_tree(nodes: list[RpAiRdgNode]) -> list[dict]:
    """Convert flat node list to nested tree (top-level only, children embedded)."""
    id_map = {n.id: n for n in nodes}
    top_level = [n for n in nodes if n.parent_id is None]
    top_level.sort(key=lambda n: n.sort_order)

    def to_dict(n: RpAiRdgNode) -> dict:
        d: dict = {
            "id": n.id,
            "nodeKey": n.node_key,
            "label": n.label,
            "type": n.node_type,
            "sortOrder": n.sort_order,
            "parentId": n.parent_id,
        }
        if n.node_type == "parallel-group":
            children = sorted(
                [c for c in nodes if c.parent_id == n.id],
                key=lambda c: c.sort_order,
            )
            d["children"] = [to_dict(c) for c in children]
        return d

    return [to_dict(n) for n in top_level]


# ── Read endpoints ────────────────────────────────────────────────────────────

@router.get("/stages")
def get_stages(db: Session = Depends(get_db)) -> list[dict]:
    """Return all 7 stages with their node trees."""
    all_nodes = db.scalars(select(RpAiRdgNode)).all()
    nodes_by_stage: dict[str, list[RpAiRdgNode]] = {}
    for n in all_nodes:
        nodes_by_stage.setdefault(n.stage_key, []).append(n)

    result = []
    for meta in STAGES_META:
        stage_nodes = nodes_by_stage.get(meta["id"], [])
        result.append({
            "id": meta["id"],
            "label": meta["label"],
            "group": meta["group"],
            "flow": _build_node_tree(stage_nodes),
        })
    return result


@router.get("/tool-mappings")
def get_tool_mappings(db: Session = Depends(get_db)) -> dict[str, list[str]]:
    """Return { node_key: [tool_name, ...] } for all mappings."""
    rows = db.scalars(select(RpAiRdgToolMapping)).all()
    result: dict[str, list[str]] = {}
    for r in rows:
        result.setdefault(r.node_key, []).append(r.tool_name)
    return result


# ── Write endpoints (rsw_admin only) ─────────────────────────────────────────

class NodeCreate(BaseModel):
    stageKey: str
    nodeKey: str
    label: str
    nodeType: str = "node"
    parentId: int | None = None
    sortOrder: int = 0


class NodeUpdate(BaseModel):
    label: str | None = None
    sortOrder: int | None = None
    parentId: int | None = None


class ToolMappingUpdate(BaseModel):
    nodeKey: str
    toolNames: list[str]


@router.get("/admins")
def list_admins(
    request: Request,
    db: Session = Depends(get_db),
) -> list[str]:
    """List RSW admin EIDs. Requires rsw_admin."""
    _require_rsw_admin(request, db)
    return [r.eid for r in db.scalars(select(RpAiRdgAdmin)).all()]


@router.post("/admins", status_code=status.HTTP_201_CREATED)
def add_admin(
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    _require_rsw_admin(request, db)
    eid = str(body.get("eid", "")).strip().upper()
    if not eid:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "eid required.")
    if db.scalar(select(RpAiRdgAdmin).where(RpAiRdgAdmin.eid == eid)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"{eid} is already an admin.")
    db.add(RpAiRdgAdmin(eid=eid))
    db.commit()
    return {"eid": eid}


@router.delete("/admins/{eid}")
def remove_admin(
    eid: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    caller = _require_rsw_admin(request, db)
    target = eid.upper()
    if caller == target:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot remove yourself.")
    row = db.scalar(select(RpAiRdgAdmin).where(RpAiRdgAdmin.eid == target))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Admin not found.")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.post("/nodes", status_code=status.HTTP_201_CREATED)
def create_node(
    body: NodeCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    _require_rsw_admin(request, db)
    if db.scalar(select(RpAiRdgNode).where(RpAiRdgNode.node_key == body.nodeKey)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"node_key '{body.nodeKey}' already exists.")
    row = RpAiRdgNode(
        stage_key=body.stageKey,
        node_key=body.nodeKey,
        label=body.label,
        node_type=body.nodeType,
        parent_id=body.parentId,
        sort_order=body.sortOrder,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "nodeKey": row.node_key}


@router.put("/nodes/{node_id}")
def update_node(
    node_id: int,
    body: NodeUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    _require_rsw_admin(request, db)
    row = db.get(RpAiRdgNode, node_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found.")
    if body.label is not None:
        row.label = body.label
    if body.sortOrder is not None:
        row.sort_order = body.sortOrder
    if body.parentId is not None:
        row.parent_id = body.parentId
    db.commit()
    return {"id": row.id, "nodeKey": row.node_key, "label": row.label}


@router.delete("/nodes/{node_id}")
def delete_node(
    node_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    _require_rsw_admin(request, db)
    row = db.get(RpAiRdgNode, node_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found.")
    # Delete associated tool mappings first.
    db.execute(delete(RpAiRdgToolMapping).where(RpAiRdgToolMapping.node_key == row.node_key))
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.put("/tool-mappings")
def update_tool_mappings(
    body: ToolMappingUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Replace the full tool list for a given node_key (or stage:xxx key)."""
    _require_rsw_admin(request, db)
    db.execute(
        delete(RpAiRdgToolMapping).where(RpAiRdgToolMapping.node_key == body.nodeKey)
    )
    for tool_name in body.toolNames:
        db.add(RpAiRdgToolMapping(node_key=body.nodeKey, tool_name=tool_name))
    db.commit()
    return {"nodeKey": body.nodeKey, "toolNames": body.toolNames}
