"""R&D Guide models: rp_ai_rdg_node and rp_ai_rdg_tool_mapping."""
from __future__ import annotations

from sqlalchemy import Enum, ForeignKey, Integer, String
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
