"""RVC 用量推送的请求／响应形状。

契约本身（字段、必填、取值）见 ``routers/external_usage.py`` 的 docstring。这里只放校验，
以及那些「为什么这样校验」不写下来就会被后人放宽的地方。
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import Field, field_serializer, field_validator, model_validator

from app.models.external_usage_event import (
    ALLOWED_KINDS,
    ALLOWED_VERB_KINDS,
    ALLOWED_VERBS,
)
from app.schemas.common import CamelModel, as_utc

#: 单批上限。定这个数是为了让 413 有个明确的边界：没有上限时，一个跑飞的批处理能用一次
#: 请求把库写满，而症状是网关超时而不是「批太大」。
MAX_EVENTS_PER_BATCH = 500

#: ``occurredAt`` 允许的秒级 epoch 下界。2020-01-01。毫秒级时间戳（13 位）落在这个下界之上
#: 很远，但把它当秒读会得到公元 5 万年 —— 那种行会静默永远留在「最近 7 天」窗口外，
#: 而不是报错。所以上界也要卡。
_EPOCH_MIN = 1_577_836_800   # 2020-01-01T00:00:00Z
_EPOCH_MAX = 4_102_444_800   # 2100-01-01T00:00:00Z


def aware_or_epoch(value: datetime | int) -> datetime:
    """统一成带时区的 ``datetime``。两个 IVC 端点共用。

    **裸本地时间一律拒。** 对方在另一个站点、另一个时区，一个不带偏移的
    ``2026-09-01T14:22:31`` 无法还原成时刻；猜一个偏移会让整批事件整体偏几小时，而几小时
    刚好够把月底那几条挪到上个月。这一条拒得比宽容更省事：现在是一个 422，宽容下去是
    一个事后无法分辨对错的报表。

    抽成模块级函数而不是在 ``routers/ivc_web_assets`` 里再写一遍：两份校验迟早漂成一份拒
    裸时间、一份猜一个偏移，而那种不一致只体现在数字上、不报错。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，挡在下面那支之前
        raise ValueError("时间不能是布尔值")
    if isinstance(value, int):
        if not _EPOCH_MIN <= value <= _EPOCH_MAX:
            raise ValueError(f"{value} 不像秒级 epoch（毫秒级请除以 1000）")
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if value.tzinfo is None:
        raise ValueError("时间必须带时区偏移，或改用秒级 epoch")
    return value


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
        """统一成带时区的 ``datetime``。逻辑在 ``aware_or_epoch``，两个端点共用。

        这里只把错误消息里的字段名补上 —— 对方拿到的提示要指向 ``occurredAt``，而不是一句
        不知道说的是哪个字段的「时间必须带时区偏移」。
        """
        try:
            return aware_or_epoch(value)
        except ValueError as exc:
            raise ValueError(f"occurredAt: {exc}") from exc

    @model_validator(mode="after")
    def _verb_fits_kind(self) -> "ExternalUsageEventIn":
        """``verb`` 与 ``kind`` 的组合必须成立。

        单独校验两个字段挡不住 ``open`` + ``skill``：两个取值各自都在闭集里，合起来才说不通
        （技能是下载的，没有"打开"这个动作）。这里拒掉，而不是让它走到落库阶段变成一条
        ``skipped`` —— ``skipped`` 的含义是"形状对、门户这边落不了地，补了数据就能重推"，
        而这种请求重推一万次也落不了地。把它标成 skipped 会让对方一直重试。
        """
        if (self.verb, self.kind) not in ALLOWED_VERB_KINDS:
            ok = ", ".join(
                f"{v}+{k}" for v, k in sorted(ALLOWED_VERB_KINDS) if v == self.verb
            )
            raise ValueError(
                f"verb={self.verb!r} 不适用于 kind={self.kind!r}"
                f"（{self.verb} 可用的组合：{ok or '无'}）"
            )
        return self


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


# --- 读回（GET） ------------------------------------------------------------

#: 一次最多回多少条。定这个数是为了让分页有个明确边界：没有上限时，一次请求可能把整张表
#: 序列化进内存，而那个失败是 502 而不是「太多了」。
MAX_FEED_LIMIT = 1000
DEFAULT_FEED_LIMIT = 500


class ExternalUsageEventOut(CamelModel):
    """回给上游的一条使用记录。

    字段与他们**推过来**的那一套对齐（``verb`` / ``kind`` / ``assetId`` / ``eid`` /
    ``occurredAt``），这样两边能直接对账，不必再写一层映射。

    没有 ``eventId``：这一条不一定来自他们的推送 —— 门户里的人自己点下载/收藏也会产生它，
    而那些行没有上游事件 id。给一个编造的 id 比留空更坏。

    ``department`` 是**组 slug**（``ivc``）而不是 LDAP 部门全称。全称里带子团队名，那本身
    就是组织结构信息，而回答「谁在用」并不需要它。
    """

    verb: str
    kind: str
    asset_id: str
    eid: str | None = None
    department: str | None = None
    occurred_at: datetime

    @field_serializer("occurred_at")
    def _utc(self, value: object) -> object:
        return as_utc(value)


class ExternalUsageFeed(CamelModel):
    """一页使用记录。

    ``total`` 是**这一页的条数**，不是符合条件的总数。故意不给总数：算它要再扫一遍，而调用方
    真正需要的是「还有没有下一页」，那由 ``total == limit`` 判断。写清楚是因为一个叫 total 的
    字段被当成全量总数去做百分比，是那种算得出数、数还错的失败。
    """

    generated_at: datetime
    total: int
    events: list[ExternalUsageEventOut] = Field(default_factory=list)

    @field_serializer("generated_at")
    def _utc(self, value: object) -> object:
        return as_utc(value)
