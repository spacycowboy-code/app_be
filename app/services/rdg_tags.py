"""资产与 R&D Guide 工具之间的关联。

一句话:上传 skill / tool / agent 时可以声明「这个资产对应 R&D Guide 上的哪几个工具」,
R&D Guide 的工具卡片就能显示「门户里有 N 个相关资产」并列出来。

三条口径,都是明确定过的、不是这里的默认值:

1. **可选,且可以选多个。** 不选就是不选,没有默认值 —— 猜一个关联比不关联更坏,它会让
   卡片上出现一个没人认领的数字。
2. **只有 RSW 的资产能挂。** 那 19 个工具全是 RSW 的,让别的部门的资产挂上去会让卡片上的
   数字含义变成「碰巧提到了这个名字的资产」。判据是资产的 ``category_id == 'radio-sw'``。
3. **候选清单从 ``rp_ai_rdg_tool_mapping`` 派生**,不是另写一份名单。那张表是 R&D Guide
   「哪个工序挂了哪些工具」的真相,它的 distinct ``tool_name`` 恰好就是页面上能看到的工具
   集合(2026-09-15 实测:库里 19 个 distinct,与前端那份硬编码的 19 条完全相等)。另写一份
   名单就是第二处真相,两份迟早不一致。

**工具改名会断开关联。** ``tool_name`` 在两张表里都是名字而不是 id,所以改名要同时改
``rp_ai_rdg_tool_mapping`` 和这里。这是选「跟着 R&D Guide 用同一个键」的代价;换成 id 的话
两边就会各自有一套工具的概念,那个更贵。改名本身今天没有接口(工具清单是前端硬编码的),
所以这条现在是理论代价。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.market import RpAiMarketItem
from app.models.rdg import RpAiRdgAssetTag, RpAiRdgToolMapping
from app.models.skill import RpAiSkill

#: 能挂关联的分类。R&D Guide 今天只有 RSW 一份,所以这是一个值而不是一张表。
RSW_CATEGORY_ID = "radio-sw"

#: 资产种类到模型的对应。``asset_kind`` 的取值由数据库的 Enum 兜着,这里只管取哪张表。
_MODEL_BY_KIND = {"skill": RpAiSkill, "item": RpAiMarketItem}


def valid_tool_names(db: Session) -> list[str]:
    """R&D Guide 上现在有哪些工具,按名字排序。

    这就是上传表单那个下拉框的内容,也是写入时的白名单 —— 同一个查询,所以下拉框里能选的
    和后端肯收的不可能不一致。
    """
    rows = db.scalars(select(RpAiRdgToolMapping.tool_name).distinct()).all()
    return sorted({r for r in rows if r})


def unknown_tool_names(db: Session, names: list[str]) -> list[str]:
    """``names`` 里有哪些不是 R&D Guide 上的工具。空列表表示全都认识。"""
    valid = set(valid_tool_names(db))
    return [n for n in names if n not in valid]


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
        # 前端只在选到 RSW 时才显示这个下拉,所以走到这里的请求要么是绕过表单,要么是先选了
        # RSW 挂上再改分类。两种都拒:那 19 个工具是 RSW 的 R&D Guide 上的东西,别的部门的
        # 资产挂上去会让卡片上的数字变成「碰巧提到这个名字的资产」。
        return "只有 Radio SW 的资产可以关联 R&D Guide 工具。"

    unknown = unknown_tool_names(db, wanted)
    if unknown:
        # 报出全部不认识的名字而不是第一个 —— 一次说清比让人试三遍好。
        return "R&D Guide 上没有这些工具:" + "、".join(unknown)
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
    用户在界面上取消勾选一个工具就没有任何办法真的取消掉。

    不 commit —— 调用方还在同一个事务里写资产本身,两者要一起成或一起不成。
    """
    wanted = sorted({n.strip() for n in names if n and n.strip()})

    db.execute(
        delete(RpAiRdgAssetTag).where(
            RpAiRdgAssetTag.asset_kind == kind,
            RpAiRdgAssetTag.asset_id == asset_id,
        )
    )
    for name in wanted:
        db.add(RpAiRdgAssetTag(asset_kind=kind, asset_id=asset_id, tool_name=name))
    db.flush()
    return wanted


def tags_for(db: Session, kind: str, asset_id: str) -> list[str]:
    """这个资产挂在哪些工具上。"""
    rows = db.scalars(
        select(RpAiRdgAssetTag.tool_name)
        .where(
            RpAiRdgAssetTag.asset_kind == kind,
            RpAiRdgAssetTag.asset_id == asset_id,
        )
        .order_by(RpAiRdgAssetTag.tool_name)
    ).all()
    return list(rows)


def forget(db: Session, kind: str, asset_id: str) -> int:
    """资产被删掉时清掉它的关联,返回清掉几行。

    这张表刻意没有外键(见模型的 docstring),所以级联删除得显式做。不清的话卡片上那个数字会
    把已经不存在的资产算进去 —— 一个点进去是空的计数比没有计数更坏。
    """
    result = db.execute(
        delete(RpAiRdgAssetTag).where(
            RpAiRdgAssetTag.asset_kind == kind,
            RpAiRdgAssetTag.asset_id == asset_id,
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


def counts_by_tool(db: Session) -> dict[str, int]:
    """每个工具下挂了几个**看得见的**门户资产。R&D Guide 卡片那一行读这个。

    只返回大于 0 的工具,所以前端拿到的键就是「该显示那一行的卡片」。0 不返回:一张写着
    「0 个相关资产」的卡片是噪声,而这一行的价值恰恰在于它出现的时候。

    三次查询而不是一句 group by:计数要过可见性(见 ``_visible_ids``),而可见性在另外两张表上。
    一句 SQL 要做两个 join 再 union,而这批数据的量级是几十行 —— 那个复杂度换不到东西。
    """
    rows = db.execute(
        select(
            RpAiRdgAssetTag.asset_kind,
            RpAiRdgAssetTag.asset_id,
            RpAiRdgAssetTag.tool_name,
        )
    ).all()
    if not rows:
        return {}

    visible = {
        kind: _visible_ids(db, kind, {r[1] for r in rows if r[0] == kind})
        for kind in _MODEL_BY_KIND
    }

    counts: dict[str, int] = {}
    for kind, asset_id, tool_name in rows:
        if asset_id in visible.get(kind, set()):
            counts[tool_name] = counts.get(tool_name, 0) + 1
    return counts


def assets_for_tool(db: Session, tool_name: str) -> list[dict]:
    """某个工具下挂着的资产清单,给卡片点开之后那一层用。

    和计数走同一道可见性过滤,所以「卡片上写 3 个」和「点开看到 3 条」不会对不上 —— 两处
    用不同判据是这类计数最常见的坏法。

    每条给出前端能直接拿去跳转的东西:``kind`` 决定去 skill 还是 market 的详情页,``id``
    是那一页的参数。名字用资产自己的名字,不是关联表里的东西。
    """
    out: list[dict] = []

    skill_ids = {
        r[0]
        for r in db.execute(
            select(RpAiRdgAssetTag.asset_id).where(
                RpAiRdgAssetTag.tool_name == tool_name,
                RpAiRdgAssetTag.asset_kind == "skill",
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
            select(RpAiRdgAssetTag.asset_id).where(
                RpAiRdgAssetTag.tool_name == tool_name,
                RpAiRdgAssetTag.asset_kind == "item",
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
