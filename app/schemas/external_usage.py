"""RVC 用量推送的请求／响应形状。

契约本身（字段、必填、取值）见 ``routers/external_usage.py`` 的 docstring。这里只放校验，
以及那些「为什么这样校验」不写下来就会被后人放宽的地方。
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import Field, field_validator

from app.models.external_usage_event import ALLOWED_KINDS, ALLOWED_VERBS
from app.schemas.common import CamelModel

#: 单批上限。定这个数是为了让 413 有个明确的边界：没有上限时，一个跑飞的批处理能用一次
#: 请求把库写满，而症状是网关超时而不是「批太大」。
MAX_EVENTS_PER_BATCH = 500

#: ``occurredAt`` 允许的秒级 epoch 下界。2020-01-01。毫秒级时间戳（13 位）落在这个下界之上
#: 很远，但把它当秒读会得到公元 5 万年 —— 那种行会静默永远留在「最近 7 天」窗口外，
#: 而不是报错。所以上界也要卡。
_EPOCH_MIN = 1_577_836_800   # 2020-01-01T00:00:00Z
_EPOCH_MAX = 4_102_444_800   # 2100-01-01T00:00:00Z


class ExternalUsageEventIn(CamelModel):
    """一条上游事件。

    没有 ``department`` 字段，这是刻意的：部门由门户按自己的用户行现取并存成快照。收对方给的
    部门等于把两套组织词表混进同一列，而报告的部门→组映射只认门户这一套。

    没有 ``version`` 字段，最后商量掉了 —— 门户数唯一下载人数用 ``count(distinct eid)``，
    两张落点表也都没有这一列。
    """

    event_id: str = Field(min_length=1, max_length=128)
    verb: str
    kind: str
    asset_id: str = Field(min_length=1, max_length=128)
    eid: str = Field(min_length=1, max_length=64)
    #: 带时区的 ISO 8601，或秒级 epoch。两种都收是当初对对方让的步。
    occurred_at: datetime | int

    @field_validator("verb")
    @classmethod
    def _known_verb(cls, value: str) -> str:
        if value not in ALLOWED_VERBS:
            # 把合法取值写进消息里。对方最可能踩的坑是照抄文档里的占位写法
            # ``download/like``，那时一句「unknown verb」帮不上任何忙。
            allowed = " | ".join(sorted(ALLOWED_VERBS))
            raise ValueError(f"unknown verb: {value!r}（合法取值：{allowed}）")
        return value

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in ALLOWED_KINDS:
            allowed = " | ".join(sorted(ALLOWED_KINDS))
            raise ValueError(f"unknown kind: {value!r}（合法取值：{allowed}）")
        return value

    @field_validator("eid")
    @classmethod
    def _upper_eid(cls, value: str) -> str:
        eid = value.strip().upper()
        if not eid:
            raise ValueError("eid 不能为空")
        return eid

    @field_validator("occurred_at")
    @classmethod
    def _aware_or_epoch(cls, value: datetime | int) -> datetime:
        """统一成带时区的 ``datetime``。

        **裸本地时间一律拒。** 对方在另一个站点、另一个时区，一个不带偏移的
        ``2026-09-01T14:22:31`` 无法还原成时刻；猜一个偏移会让整批事件整体偏几小时，而几小时
        刚好够把月底那几条挪到上个月。这一条拒得比宽容更省事：现在是一个 422，宽容下去是
        一个事后无法分辨对错的报表。
        """
        if isinstance(value, bool):  # bool 是 int 的子类，挡在下面那支之前
            raise ValueError("occurredAt 不能是布尔值")
        if isinstance(value, int):
            if not _EPOCH_MIN <= value <= _EPOCH_MAX:
                raise ValueError(
                    f"occurredAt={value} 不像秒级 epoch（毫秒级请除以 1000）"
                )
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if value.tzinfo is None:
            raise ValueError("occurredAt 必须带时区偏移，或改用秒级 epoch")
        return value


class ExternalUsageBatchIn(CamelModel):
    """一批。

    批量而不是一条一请求：对方是批处理，一条一请求等于几千次握手。
    """

    events: list[ExternalUsageEventIn] = Field(min_length=1)


class ExternalUsageOutcome(CamelModel):
    """逐条结果。字段名照 ``ivc_sync.IvcSyncOutcome``，对方已经在解析那个形状了。"""

    event_id: str
    action: str
    detail: str | None = None


class ExternalUsageResult(CamelModel):
    """整批结果。

    计数和逐条结果都给。只给计数的话，「有 3 条 skipped」这句话回答不了「哪 3 条」，而
    对方要重推就必须知道是哪几条。
    """

    received: int
    applied: int = 0
    duplicate: int = 0
    skipped: int = 0
    outcomes: list[ExternalUsageOutcome] = Field(default_factory=list)
