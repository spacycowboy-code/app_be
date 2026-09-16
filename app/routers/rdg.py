"""R&D Guide endpoints.

GET  /api/rsw/rdg/stages          — full stage+node tree (all authenticated users)
GET  /api/rsw/rdg/tool-mappings   — node_key → [tool_name] map (all authenticated users)
GET  /api/rsw/rdg/tools           — the tool names an asset may be tagged with
GET  /api/rsw/rdg/tool-assets     — tool_name → count of portal assets tagged with it
GET  /api/rsw/rdg/tool-assets/{tool_name} — the assets behind one of those counts
GET  /api/rsw/rdg/asset-tags/{kind}/{id}  — which tools one asset is tagged with
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

from app.config import get_settings
from app.database import get_db
from app.models.rdg import RpAiRdgAdmin, RpAiRdgNode, RpAiRdgToolMapping
from app.services import rdg_tags, rsw_admin

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
    """保留这个名字给本模块内部用,判断委托给服务层。

    判断本身挪进 ``services/rsw_admin.py`` 是因为 ``/api/auth/me`` 也要同一个答案 —— 两处各写
    一遍会让界面显示一个按钮而后端拒绝它,且两边都不报错。
    """
    return rsw_admin.is_rsw_admin(db, eid)


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


@router.get("/tools")
def get_tools(db: Session = Depends(get_db)) -> list[str]:
    """上传表单那个「关联 R&D Guide 工具」下拉框的内容。

    值是 ``tool-mappings`` 的 distinct ``tool_name``,不是另写的一份名单 —— 写入时的白名单
    走同一个函数,所以下拉框里能选的和后端肯收的不可能不一致。见 ``services/rdg_tags.py``。
    """
    return rdg_tags.valid_tool_names(db)


@router.get("/tool-assets")
def get_tool_asset_counts(db: Session = Depends(get_db)) -> dict[str, int]:
    """{ tool_name: 挂在它下面的门户资产数 },R&D Guide 卡片那一行读这个。

    只返回有资产的工具,所以前端拿到的键就是「该显示那一行的卡片」。数字为 0 的不返回:
    一张写着「0 个相关资产」的卡片是噪声。
    """
    return rdg_tags.counts_by_tool(db)


@router.get("/tool-assets/{tool_name:path}")
def get_tool_assets(tool_name: str, db: Session = Depends(get_db)) -> list[dict]:
    """某个工具下挂着的资产清单,卡片点开之后那一层。

    路径参数用 ``:path`` 是因为工具名字里有空格和别的字符(``AC Onboarding Assistant``、
    ``DP Slot Info Decode``),名字本身就是这套东西的键 —— 见 ``services/rdg_tags.py`` 里
    关于「为什么用名字不用 id」的注释。

    **工具名字不存在与工具下没有资产,都返回空列表。** 不区分是刻意的:这个端点的调用方是
    卡片,而卡片的名字来自 tool-mappings,不可能问出一个不存在的名字;真会问出来的场景是
    工具刚被改名,那时候「空」正是事实。
    """
    return rdg_tags.assets_for_tool(db, tool_name)


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


@router.get("/asset-tags/{kind}/{asset_id}")
def get_asset_tags(
    kind: str, asset_id: str, db: Session = Depends(get_db)
) -> list[str]:
    """一个资产现在挂在哪些工具上。

    编辑弹窗回填用。**没有这个端点,「挂错了」就没有出路** —— 编辑接口把 ``rdgTools`` 当成
    整体替换,前端不知道原值就只能要么不发(不改)要么发一份新的(覆盖),没法在原来的基础上改。

    ``kind`` 只认 ``skill`` 与 ``item``,别的返回空列表而不是 400:调用方是前端两个已知的
    弹窗,传别的值是代码错误,而这个端点的答案对代码错误来说「空」比一个错误码更好读。
    """
    if kind not in ("skill", "item"):
        return []
    return rdg_tags.tags_for(db, kind, asset_id)


@router.get("/admins")
def list_admins(
    request: Request,
    db: Session = Depends(get_db),
) -> list[dict]:
    """谁能编辑 R&D Guide,带来源。需要 rsw_admin。

    返回 ``[{eid, source}]`` 而不是一串工号:权限有两条来源(表里显式加的、以及门户 admin 名单),
    而**只有显式那种能在界面上移除**。不带来源的话界面只能给所有人画一个移除按钮,而按在门户
    admin 上会 404 —— 一个点了报错的按钮比没有按钮更坏。

    这也修掉一个更早的问题:在门户 admin 也算 RSW admin 之前,一个门户 admin 打开这一页会看到
    一份不包含自己的名单,而他自己其实编辑得了 —— 一份与事实不符的名单。
    """
    _require_rsw_admin(request, db)
    return rsw_admin.effective_admins(db)


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
        # 分开说:这个人是门户 admin(有权限但没有可删的行)和这个人压根没有权限,是两种情况,
        # 而对界面来说前者需要一句能读懂的解释。
        if target in [e.upper() for e in get_settings().rsw_page_eid_list]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{target} 的权限来自门户 admin 名单,不在这里管理 —— "
                "要收走就把他从门户 admin 名单里去掉。",
            )
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
