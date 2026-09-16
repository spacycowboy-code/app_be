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


class RpAiRdgAssetNode(Base):
    """门户里的一个数字资产声明它对应 R&D Guide 上的哪一道**流程节点**。

    读法是「``asset_id`` 这个资产用在 ``node_key`` 这道工序上」,例如某个 skill 用在 ``pcs``
    (PCS) 或 ``oa-scope`` (OA Scope) 上。一个资产可以有多行 —— 一个工具跨多道工序是常态。

    **这张表取代了 ``rp_ai_rdg_asset_tag``(2026-09-16)。** 那张表挂的是 ``tool_name``,也就是
    关到了 R&D Guide 最里面那一层。真实结构是三层:

        阶段(7) -> 流程节点(78 个可选) -> 工具(RpAiRdgToolMapping 把工具挂到节点上)

    Miao 的原话:「本来就是数字资产(tool/skill/agent)和 workflow 的关联」。关到工具那一层是错的:
    工具是别人挂在工序上的东西,门户资产和它是兄弟关系而不是从属关系。旧表**没有删**,只是没有
    代码再读它 —— 见 ``migrate.py`` 里那段一次性搬迁。

    **``node_key`` 是名字而不是外键,和 ``RpAiRdgToolMapping.node_key`` 一样。** 那张表也是按
    node_key 挂的。这里跟着用同一个键,是为了让「工具挂在哪道工序上」和「资产用在哪道工序上」
    指向同一个东西 —— 换成 ``RpAiRdgNode.id`` 的话两边就会各有一套工序的概念。代价是节点被删
    时要显式清这里的行。

    **``asset_kind`` + ``asset_id`` 是刻意的多态,没有外键。** skill 在 ``rp_ai_skill``、
    tool/agent 在 ``rp_ai_market_item``,是两张表,而这个功能对两者一视同仁。拆成两张关联表
    能拿到外键,代价是每个读取路径都要查两次再合并。这里换成一张表 + 一个种类列,读的时候按
    种类分派(见 ``services/rdg_tags.py`` 的 ``_MODEL_BY_KIND``)。

    没有外键意味着两件事都要显式做:删资产时清这里的行(``rdg_tags.forget``),以及计数时
    过一道可见性(``rdg_tags._visible_ids``)—— 两种资产的"删除"都不真的删行。
    """

    __tablename__ = "rp_ai_rdg_asset_node"
    __table_args__ = (
        # 同一个资产对同一道工序只该有一行。靠约束而不是靠写入侧记得先查一遍:
        # 上传和「改元数据」是两条路径,两处都记得的前提迟早不成立。
        UniqueConstraint("asset_kind", "asset_id", "node_key", name="uq_rdg_asset_node"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 'skill' -> rp_ai_skill.id,'item' -> rp_ai_market_item.id
    asset_kind: Mapped[str] = mapped_column(
        Enum("skill", "item", name="rdg_asset_kind"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 节点卡片那一行按 node_key 统计,所以这一列要有索引。长度跟着 RpAiRdgNode.node_key。
    node_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)


class RpAiRdgAssetTag(Base):
    """**已废弃(2026-09-16),没有任何代码再读它。** 被 ``RpAiRdgAssetNode`` 取代。

    它挂的是 ``tool_name``,即关到了 R&D Guide 最里面那一层(工具),而需求是关到流程节点。
    保留这个类只为两件事:``migrate.py`` 里那段一次性搬迁要读它,以及在有人来问「那张表是
    什么」时代码里答得出来。表本身没删 —— 删表不可逆,而它只有个位数的行,留着的成本近乎零。
    等搬迁在生产上确认无误、且不再需要回退,再单独走一次改动删掉。
    """

    __tablename__ = "rp_ai_rdg_asset_tag"
    __table_args__ = (
        UniqueConstraint("asset_kind", "asset_id", "tool_name", name="uq_rdg_asset_tag"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    asset_kind: Mapped[str] = mapped_column(
        Enum("skill", "item", name="rdg_asset_kind"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
