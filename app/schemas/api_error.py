"""Admin 接口报错页的响应模型。

时间列要自己挂 ``as_utc``：``CamelModel`` 的那个 ``field_serializer`` 只覆盖
``created_at`` / ``updated_at`` / ``content_updated_at`` 三个名字，而这张表的时间列叫
``first_seen`` / ``last_seen`` / ``diagnosed_at`` / ``resolved_at``。不挂的话发出去的是
不带时区的裸值，浏览器会按**自己**的时区读，于是页面上的时间会静默偏掉库与浏览器之间的
时差（库在 UTC+2）——这个错不报任何异常。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import field_serializer

from app.schemas.common import CamelModel, as_utc


class Diagnosis(CamelModel):
    """LLM 给的四段。四段都空时接口回 ``null`` 而不是一个空对象 —— 见 ApiErrorRead。"""

    user: str | None = None
    cause: str | None = None
    scope: str | None = None
    action: str | None = None


class ApiErrorRead(CamelModel):
    id: int
    fingerprint: str
    method: str
    path: str
    status: int
    exc_type: str | None = None
    exc_message: str | None = None
    traceback: str | None = None
    count: int
    first_seen: datetime
    last_seen: datetime
    last_eid: str | None = None
    # 还没诊断、或诊断失败时为 null。前端据此决定显示「等待诊断」还是四段内容，所以
    # 空对象是不行的：``{user: null, ...}`` 与「有结论但内容为空」在页面上分不开。
    diagnosis: Diagnosis | None = None
    diagnosed_at: datetime | None = None
    diagnosis_error: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    @field_serializer("first_seen", "last_seen", "diagnosed_at", "resolved_at")
    def _serialize_utc(self, value: object) -> object:
        return as_utc(value)

    @classmethod
    def of(cls, row: object) -> "ApiErrorRead":
        """从 ORM 行构造，顺手把四列折成 ``diagnosis``。"""
        parts = {
            "user": getattr(row, "diagnosis_user", None),
            "cause": getattr(row, "diagnosis_cause", None),
            "scope": getattr(row, "diagnosis_scope", None),
            "action": getattr(row, "diagnosis_action", None),
        }
        diagnosis = Diagnosis(**parts) if any(parts.values()) else None
        return cls(
            id=row.id,
            fingerprint=row.fingerprint,
            method=row.method,
            path=row.path,
            status=row.status,
            exc_type=row.exc_type,
            exc_message=row.exc_message,
            traceback=row.traceback,
            count=row.count,
            first_seen=row.first_seen,
            last_seen=row.last_seen,
            last_eid=row.last_eid,
            diagnosis=diagnosis,
            diagnosed_at=row.diagnosed_at,
            diagnosis_error=row.diagnosis_error,
            resolved_at=row.resolved_at,
            resolved_by=row.resolved_by,
        )


class ApiErrorList(CamelModel):
    items: list[ApiErrorRead]
    total: int
