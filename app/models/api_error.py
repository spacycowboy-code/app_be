"""接口报错投影: ``rp_ai_api_error``。

**一行一个指纹,不是一行一次请求。** 这一页是给运维看「哪些接口在坏」的,按指纹聚合才不会
随流量无界增长 —— 一个坏掉的接口被三百个人点到,运维要看的仍然是一条。代价是丢失逐次明细,
用 ``count`` + ``first_seen`` + ``last_seen`` + ``last_eid`` + 最近一次的 ``traceback``
补偿。真要逐次追溯,edge 的访问日志仍然在,那才是完整记录;这张表是它的可读投影。

指纹 = ``(method, 去掉 query 的 path, exc_type)`` 的 sha256 前 16 位。

**path 只去掉 query string,不把 id 段替换成占位符。** 那样做要猜「哪一段是 id」,而猜错
不报错、只是把两个不相关的故障并成一条。而且 ``/api/skills/foo`` 与 ``/api/skills/bar``
本来就该是两条:同一个端点、不同的数据行,大概率是不同的根因(前者的数据坏了不代表后者也坏)。
代价是某个端点被大量不同 id 打爆时会产生很多行 —— 那种情况本身就值得单独看见,而不是被
一条聚合行藏起来。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RpAiApiError(Base):
    __tablename__ = "rp_ai_api_error"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 见模块 docstring。unique 是这张表的核心约束:记录侧靠它把「同一件事」并到一行。
    fingerprint: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )

    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)

    # 异常类名与消息。正常返回的 5xx(如显式 ``HTTPException(503)``)没有异常对象,两列为空 ——
    # 那不是缺陷,是这次失败确实不是崩溃。判定器的提示词里要能区分这两种。
    exc_type: Mapped[str | None] = mapped_column(String(120))
    exc_message: Mapped[str | None] = mapped_column(Text)
    # 最近一次的调用栈,已滤掉框架帧(见 services/api_errors.py::_app_frames)。
    traceback: Mapped[str | None] = mapped_column(Text)

    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    # 列表按它倒序,所以带索引。
    last_seen: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    # 最近一次触发者。聚合成一行之后这里只剩「最后那个人」,不是全部受影响的人。
    last_eid: Mapped[str | None] = mapped_column(String(64))

    # LLM 诊断的四段。拆成四列而不是一个 JSON 字段:页面要分别渲染,而且「给用户的一句话」
    # 是要被复制出去转发的,单独一列比从 JSON 里挖一个键清楚。
    diagnosis_user: Mapped[str | None] = mapped_column(Text)
    diagnosis_cause: Mapped[str | None] = mapped_column(Text)
    diagnosis_scope: Mapped[str | None] = mapped_column(Text)
    diagnosis_action: Mapped[str | None] = mapped_column(Text)
    diagnosed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 诊断为什么没成功。没有这一列,LLM 不可用时页面上就是一片空白,而空白读起来像
    # 「还没轮到它」——两者的处理方式完全不同(等 / 去查判定器)。
    diagnosis_error: Mapped[str | None] = mapped_column(String(300))

    # 人工标记已处理。不删行:删掉之后同一个指纹再来会被当成新问题,而「这个我看过了」
    # 恰恰是下次再来时最该知道的事。
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
