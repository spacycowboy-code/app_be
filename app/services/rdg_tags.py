"""资产与 R&D Guide 工具之间的关联。

一句话:上传 skill / tool / agent 时可以声明「这个资产用在 R&D Guide 的哪几道流程节点上」,
那道节点的卡片就能显示「门户里有 N 个相关资产」并列出来。

**2026-09-16 改过一次关联的对象,这是本模块最重要的一条。** 原先关的是工具名
(``rp_ai_rdg_asset_tag.tool_name``),那是错的一层。R&D Guide 的真实结构是三层:

    阶段(7) -> 流程节点(78 个可选) -> 工具(``rp_ai_rdg_tool_mapping`` 把工具挂到节点上)

Miao 的原话:「本来就是数字资产(tool/skill/agent)和 workflow 的关联」,例子是 PCS、OA Scope
—— 那都是**节点**。工具是别人挂在工序上的东西,门户资产和它是兄弟关系而不是从属关系。现在关
的是 ``node_key``,写进 ``rp_ai_rdg_asset_node``;旧表留着但没有代码读它(见 ``models/rdg.py``)。

三条口径,都是明确定过的、不是这里的默认值:

1. **可选,且可以选多个。** 不选就是不选,没有默认值 —— 猜一个关联比不关联更坏,它会让
   卡片上出现一个没人认领的数字。
2. **只有 RSW 的资产能挂。** 那些流程节点全是 RSW 的 R&D Guide 上的,让别的部门的资产挂上去
   会让卡片上的数字含义变成「碰巧引用了这个 key 的资产」。判据是资产的
   ``category_id == 'radio-sw'``。
3. **候选清单从 ``rp_ai_rdg_node`` 派生**,不是另写一份名单。那张表就是 R&D Guide 的流程本身。
   另写一份名单就是第二处真相,两份迟早不一致。

**容器节点不能被选。** ``parallel-group`` 是排版用的容器(它自己的 label 是空的),把资产挂上去
等于挂到"一组工序"而不是某一道,卡片不知道该显示在哪。所以白名单要按 ``node_type`` 过滤,
只判断"表里有没有这一行"是不够的。

**节点被删会留下孤儿行。** ``node_key`` 在两张表里都是 key 而不是外键,这是选「跟着 R&D Guide
用同一个键」的代价;换成 ``RpAiRdgNode.id`` 的话两边就会各自有一套工序的概念,那个更贵。
计数那一侧不受影响(只统计白名单里的 key),但清理要显式做。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.market import RpAiMarketItem
from app.models.rdg import RpAiRdgAssetNode, RpAiRdgNode
from app.models.skill import RpAiSkill

#: 能挂关联的分类。R&D Guide 今天只有 RSW 一份,所以这是一个值而不是一张表。
RSW_CATEGORY_ID = "radio-sw"

#: 资产种类到模型的对应。``asset_kind`` 的取值由数据库的 Enum 兜着,这里只管取哪张表。
_MODEL_BY_KIND = {"skill": RpAiSkill, "item": RpAiMarketItem}


#: 不能被关联的节点类型。``parallel-group`` 是排版用的容器(它自己连 label 都是空的),
#: 把资产挂到容器上等于挂到"一组工序"而不是某一道 —— 卡片不知道该显示在哪。
_NON_SELECTABLE_NODE_TYPES = {"parallel-group"}


def selectable_nodes(db: Session) -> list[dict]:
    """R&D Guide 上可以被关联的流程节点,按阶段与流程顺序排。

    **这是上传表单那个选择器的内容,也是写入时的白名单 —— 同一个查询。** 所以界面上能选的
    和后端肯收的不可能不一致;两处各查一次的话迟早出现"选得上但存不进去"。

    每条带上 ``stageKey``,因为 78 个节点平铺成一列是不能用的,选择器要按阶段分组。
    """
    rows = db.scalars(select(RpAiRdgNode).order_by(RpAiRdgNode.stage_key, RpAiRdgNode.sort_order)).all()
    return [
        {"nodeKey": n.node_key, "label": n.label, "stageKey": n.stage_key, "nodeType": n.node_type}
        for n in rows
        if n.node_type not in _NON_SELECTABLE_NODE_TYPES and (n.label or "").strip()
    ]


def valid_node_keys(db: Session) -> list[str]:
    """可以被关联的 node_key,排序稳定。"""
    return sorted({n["nodeKey"] for n in selectable_nodes(db)})


def unknown_node_keys(db: Session, keys: list[str]) -> list[str]:
    """``keys`` 里有哪些不是可关联的流程节点。空列表表示全都认识。

    容器节点(``parallel-group``)在这里也算"不认识"—— 它确实存在于 ``rp_ai_rdg_node`` 里,
    但不是一道工序。只按"表里有没有这一行"判断会把容器放进来。
    """
    valid = set(valid_node_keys(db))
    return [k for k in keys if k not in valid]


def rejection_for(db: Session, names: list[str], category_id: str | None) -> str | None:
    """这批关联能不能写进这个分类的资产。不能就返回一句给人看的话,能就返回 ``None``。

    放在这里而不是各个上传端点里,是因为有三条写入路径(skill 上传、tool/agent 上传、改元数据)
    而措辞和判据应该只有一份。**返回话而不是抛异常**,是为了让这个模块不依赖 fastapi ——
    HTTP 状态码是路由那一层的事。

    空清单一律放行:「不选」是这个字段的正常状态,不是错误。
    """
    wanted = [n.strip() for n in names if n and n.strip()]
    if not wanted:
        return None

    if (category_id or "") != RSW_CATEGORY_ID:
        # 前端只在选到 RSW 时才显示这个选择器,所以走到这里的请求要么是绕过表单,要么是先选了
        # RSW 挂上再改分类。两种都拒:那些流程节点是 RSW 的 R&D Guide 上的东西,别的部门的
        # 资产挂上去会让节点卡片上的数字变成「碰巧引用了这个 key 的资产」。
        return "只有 Radio SW 的资产可以关联 R&D Guide 的流程节点。"

    unknown = unknown_node_keys(db, wanted)
    if unknown:
        # 报出全部不认识的 key 而不是第一个 —— 一次说清比让人试三遍好。
        return "R&D Guide 上没有这些流程节点:" + "、".join(unknown)
    return None


def _asset_category(db: Session, kind: str, asset_id: str) -> str | None:
    model = _MODEL_BY_KIND.get(kind)
    if model is None:
        return None
    row = db.get(model, asset_id)
    return getattr(row, "category_id", None) if row is not None else None


def is_taggable(db: Session, kind: str, asset_id: str) -> bool:
    """这个资产能不能挂关联 —— 即它是不是 RSW 的。"""
    return _asset_category(db, kind, asset_id) == RSW_CATEGORY_ID


def set_tags(db: Session, kind: str, asset_id: str, names: list[str]) -> list[str]:
    """把这个资产的关联**整体替换**成 ``names``,返回替换后的结果。

    替换而不是追加,因为调用方(上传、改元数据)拿到的是表单上那个多选框的完整状态。追加的话
    用户在界面上取消勾选一道工序就没有任何办法真的取消掉。

    不 commit —— 调用方还在同一个事务里写资产本身,两者要一起成或一起不成。
    """
    wanted = sorted({n.strip() for n in names if n and n.strip()})

    db.execute(
        delete(RpAiRdgAssetNode).where(
            RpAiRdgAssetNode.asset_kind == kind,
            RpAiRdgAssetNode.asset_id == asset_id,
        )
    )
    for node_key in wanted:
        db.add(RpAiRdgAssetNode(asset_kind=kind, asset_id=asset_id, node_key=node_key))
    db.flush()
    return wanted


def tags_for(db: Session, kind: str, asset_id: str) -> list[str]:
    """这个资产用在哪些流程节点上,返回 node_key 列表。"""
    rows = db.scalars(
        select(RpAiRdgAssetNode.node_key)
        .where(
            RpAiRdgAssetNode.asset_kind == kind,
            RpAiRdgAssetNode.asset_id == asset_id,
        )
        .order_by(RpAiRdgAssetNode.node_key)
    ).all()
    return list(rows)


def forget(db: Session, kind: str, asset_id: str) -> int:
    """资产被删掉时清掉它的关联,返回清掉几行。

    这张表刻意没有外键(见模型的 docstring),所以级联删除得显式做。不清的话卡片上那个数字会
    把已经不存在的资产算进去 —— 一个点进去是空的计数比没有计数更坏。
    """
    result = db.execute(
        delete(RpAiRdgAssetNode).where(
            RpAiRdgAssetNode.asset_kind == kind,
            RpAiRdgAssetNode.asset_id == asset_id,
        )
    )
    return int(result.rowcount or 0)


def _visible_ids(db: Session, kind: str, ids: set[str]) -> set[str]:
    """``ids`` 里哪些资产现在在市场上看得见,即 ``status == 'approved'``。

    **计数必须过这一道。** 两种资产的"删除"都不真的删行:技能下架是置 ``archived``(行带着
    下载量、收藏、审计记录,删了就没了),市场条目被拒是 ``pending`` / 被删才真的没。所以只按
    关联表计数会把已经从市场上消失的资产算进卡片上那个数字 —— 而那个数字点进去是空的,比没有
    这个数字更坏。
    """
    if not ids:
        return set()
    model = _MODEL_BY_KIND.get(kind)
    if model is None:
        return set()
    rows = db.execute(
        select(model.id).where(model.id.in_(list(ids)), model.status == "approved")
    ).all()
    return {r[0] for r in rows}


def counts_by_node(db: Session) -> dict[str, int]:
    """每道工序上用了几个**看得见的**门户资产。R&D Guide 节点卡片那一行读这个。

    只返回大于 0 的节点,所以前端拿到的键就是「该显示那一行的卡片」。0 不返回:一张写着
    「0 个相关资产」的卡片是噪声,而这一行的价值恰恰在于它出现的时候。

    三次查询而不是一句 group by:计数要过可见性(见 ``_visible_ids``),而可见性在另外两张表上。
    一句 SQL 要做两个 join 再 union,而这批数据的量级是几十行 —— 那个复杂度换不到东西。
    """
    rows = db.execute(
        select(
            RpAiRdgAssetNode.asset_kind,
            RpAiRdgAssetNode.asset_id,
            RpAiRdgAssetNode.node_key,
        )
    ).all()
    if not rows:
        return {}

    visible = {
        kind: _visible_ids(db, kind, {r[1] for r in rows if r[0] == kind})
        for kind in _MODEL_BY_KIND
    }

    counts: dict[str, int] = {}
    for kind, asset_id, node_key in rows:
        if asset_id in visible.get(kind, set()):
            counts[node_key] = counts.get(node_key, 0) + 1
    return counts


def assets_for_node(db: Session, node_key: str) -> list[dict]:
    """某道工序上用着的资产清单,给卡片点开之后那一层用。

    和计数走同一道可见性过滤,所以「卡片上写 3 个」和「点开看到 3 条」不会对不上 —— 两处
    用不同判据是这类计数最常见的坏法。

    每条给出前端能直接拿去跳转的东西:``kind`` 决定去 skill 还是 market 的详情页,``id``
    是那一页的参数。名字用资产自己的名字,不是关联表里的东西。
    """
    out: list[dict] = []

    skill_ids = {
        r[0]
        for r in db.execute(
            select(RpAiRdgAssetNode.asset_id).where(
                RpAiRdgAssetNode.node_key == node_key,
                RpAiRdgAssetNode.asset_kind == "skill",
            )
        ).all()
    }
    for s in db.scalars(
        select(RpAiSkill).where(RpAiSkill.id.in_(list(_visible_ids(db, "skill", skill_ids))))
    ).all() if skill_ids else []:
        out.append({
            "kind": "skill",
            "id": s.id,
            "name": s.name,
            "itemType": "skill",
            "description": s.description,
        })

    item_ids = {
        r[0]
        for r in db.execute(
            select(RpAiRdgAssetNode.asset_id).where(
                RpAiRdgAssetNode.node_key == node_key,
                RpAiRdgAssetNode.asset_kind == "item",
            )
        ).all()
    }
    for it in db.scalars(
        select(RpAiMarketItem).where(
            RpAiMarketItem.id.in_(list(_visible_ids(db, "item", item_ids)))
        )
    ).all() if item_ids else []:
        out.append({
            "kind": "item",
            "id": it.id,
            "name": it.name,
            "itemType": it.item_type,
            "description": it.description,
        })

    # 按名字排,让同一个工具下的清单每次打开顺序一样。两张表的结果合起来之后才排,否则
    # skill 会永远排在 tool/agent 前面 —— 那是表的顺序,不是任何人想看到的顺序。
    out.sort(key=lambda a: (a["name"] or "").lower())
    return out
