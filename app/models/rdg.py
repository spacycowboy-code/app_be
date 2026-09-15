"""R&D Guide models: rp_ai_rdg_node and rp_ai_rdg_tool_mapping."""
from __future__ import annotations

from sqlalchemy import Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RpAiRdgNode(Base):
    __tablename__ = "rp_ai_rdg_node"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    stage_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    node_type: Mapped[str] = mapped_column(
        Enum("node", "parallel-group", "continuous", name="rdg_node_type"),
        nullable=False,
        default="node",
    )
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("rp_ai_rdg_node.id", ondelete="CASCADE"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    children: Mapped[list[RpAiRdgNode]] = relationship(
        "RpAiRdgNode",
        foreign_keys=[parent_id],
        order_by="RpAiRdgNode.sort_order",
        cascade="all, delete-orphan",
    )


class RpAiRdgToolMapping(Base):
    __tablename__ = "rp_ai_rdg_tool_mapping"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # node_key matches rp_ai_rdg_node.node_key, OR 'stage:<stage_id>' for stage-level fallback
    node_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(256), nullable=False)


class RpAiRdgAdmin(Base):
    """EIDs allowed to edit R&D Guide nodes and tool mappings."""
    __tablename__ = "rp_ai_rdg_admin"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    eid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class RpAiRdgAssetTag(Base):
    """门户里的一个资产声明它对应 R&D Guide 上的哪个工具。

    读法是「``asset_id`` 这个资产对应 ``tool_name`` 这个工具」。一个资产可以有多行
    (一个工具在多个工序上出现是常态,反过来一个资产也可能同时属于两个工具),所以这是
    一张关联表而不是资产表上的一列。

    **``tool_name`` 是名字而不是外键,和 ``RpAiRdgToolMapping.tool_name`` 一样。** 那张表
    也是按名字挂的,R&D Guide 页面拿名字去查工具详情。这里跟着用名字,是为了让「挂到工序上」
    和「资产对应它」指向同一个东西 —— 如果这里换成某个 id,两边就会各自有一套工具的概念。
    代价是工具改名要同时改两张表,已写在 ``services/rdg_tags.py`` 的注释里。

    **``asset_kind`` + ``asset_id`` 是刻意的多态,没有外键。** skill 在 ``rp_ai_skill``、
    tool/agent 在 ``rp_ai_market_item``,是两张表,而这个功能对两者一视同仁。拆成两张关联表
    能拿到外键,代价是每个读取路径都要查两次再合并、以及「一个资产挂在哪些工具上」这种问题
    要先知道它是哪一种。这里换成一张表 + 一个种类列,读的时候按种类分派(见
    ``services/rdg_tags.py`` 的 ``_MODEL_BY_KIND``)。

    没有外键意味着两件事都要显式做:删资产时清这里的行(``rdg_tags.forget``),以及计数时
    过一道可见性(``rdg_tags._visible_ids``)—— 两种资产的"删除"都不真的删行。
    """

    __tablename__ = "rp_ai_rdg_asset_tag"
    __table_args__ = (
        # 同一个资产对同一个工具只该有一行。靠约束而不是靠写入侧记得先查一遍:
        # 上传和「改元数据」是两条路径,两处都记得的前提迟早不成立。
        UniqueConstraint("asset_kind", "asset_id", "tool_name", name="uq_rdg_asset_tag"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 'skill' -> rp_ai_skill.id,'item' -> rp_ai_market_item.id
    asset_kind: Mapped[str] = mapped_column(
        Enum("skill", "item", name="rdg_asset_kind"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 卡片那一行按 tool_name 统计,所以这一列要有索引。
    tool_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
