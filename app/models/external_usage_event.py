"""上游推过来的用量事件流水：``rp_ai_external_usage_event``。

RVC 把「谁什么时候下载/点赞/取消点赞了哪个资产」推到
``POST /api/integrations/ivc/usage/events``，每条先原样落这张表，再去改门户自己的那几张
业务表（``rp_ai_skill_download`` / ``rp_ai_user_favorite`` / 市场侧两张同形表）。

**为什么要这张表，而不是只改业务表。** 两个理由，缺一个都会出事：

1. **幂等。** ``(source, event_id)`` 上有唯一约束。我们提交成功后网络超时，对方必然重推，
   没有这张表那批下载就翻倍 —— 而翻倍后的月报看起来完全正常，没有任何一处会报错。
   契约里 ``eventId`` 是必填项，正是为了让这个约束立得住。
2. **点赞的历史。** ``rp_ai_user_favorite`` 是**状态**（``UniqueConstraint(eid, skill_id)``），
   只回答「现在收藏着没有」。门户自己的收藏功能就因为这个设计，永久丢掉了所有取消收藏的
   时间。这里 ``like`` / ``unlike`` 各是一行，撤销这件事有记录。

**为什么不复用 ``rp_ai_usage_event``。** 那张表的动词集是闭的（``tool_run`` /
``tool_open`` / ``install_command_copied``），而且它的 docstring 明确写着它只装「没有专用表
可去的东西」。下载和收藏都有专用表，而且 ``grafana/dashboards/skill-metrics.json`` 直接查
``rp_ai_skill_download`` —— 进通用表意味着那些图看不到 RVC 的量。

**为什么不加 ``version`` 列。** 契约里最后商量掉了。门户自己数唯一下载人数用的是
``count(distinct eid)``（``services/download_counts.py``），从来不看版本，所以两边口径本来
一致；``rp_ai_skill_download`` 和 ``rp_ai_user_favorite`` 也都没有这一列。加一列只在这张表里
存着、下游没人读，属于那种「看起来能用其实没人消费」的死字段。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

#: 推送方标识。今天只有一个，留列是因为第二个上游出现时，幂等键必须按来源分开 ——
#: 两家的 ``eventId`` 各自唯一，合在一起就不一定了。
SOURCE_RVC = "nexus-ivc"

#: 动词闭集。未知动词整条拒（422）而不是入库：一个拼错的动词会静默造出第二个分类，
#: 之后每个聚合都少算，而没有任何一处会报错。
VERB_DOWNLOAD = "download"
VERB_LIKE = "like"
VERB_UNLIKE = "unlike"
#: 访问量：对方那边有人打开了一个 web 型资产（``service_type=cloud``，用户点开就跳到
#: ``serviceUrl``）。这类资产没有下载动作，所以它是它唯一的使用信号。
#:
#: **它落 ``rp_ai_usage_event``（``verb=tool_open``），不落这张表的业务侧。** 这和
#: download / like 的处理刚好相反,理由是同一条:落点要选那个已经被下游消费的表。
#: download 有 ``rp_ai_skill_download`` / ``rp_ai_market_download`` 两张专用表、Grafana 直接
#: 查它们;而"打开"的专用落点本来就是 ``rp_ai_usage_event`` 的 ``tool_open`` —— 门户自己记的
#: 那 646 条就在那里,部门页与月报读的也是它。所以推过来的 open 进那张表,两边口径自动一致,
#: 不需要在报告侧再加一条分支。
#:
#: 外部契约里叫 ``open`` 而不是 ``tool_open``:对方推的是 agent 也是 tool,动词名里带 tool
#: 会读成"只适用于 tool"。映射写在 ``routers/external_usage._apply``。
VERB_OPEN = "open"
ALLOWED_VERBS = frozenset({VERB_DOWNLOAD, VERB_LIKE, VERB_UNLIKE, VERB_OPEN})

#: 资产类型。``skill`` 落技能两张表，``agent`` 落市场两张表。
KIND_SKILL = "skill"
KIND_AGENT = "agent"
#: ``tool`` 与 ``agent`` 在门户里是同一张表的两种 ``item_type``,所以落点相同;分开收是因为
#: 报告按 ``item_type`` 分列,而对方最清楚自己那个资产是哪一种 —— 让我们去猜就会猜错。
KIND_TOOL = "tool"
ALLOWED_KINDS = frozenset({KIND_SKILL, KIND_AGENT, KIND_TOOL})

#: 哪些 (verb, kind) 组合成立。``open`` 只对市场条目成立:技能是下载的,``tool_open`` 的
#: ``object_type`` 闭集里也没有技能这一项(见 ``usage_event.ALLOWED_OBJECT_TYPES``)。
#: 不成立的组合是 422 而不是 skipped —— skipped 意味着"形状对、落不了地",而这种是形状本身
#: 说不通,收下来只会在报表里变成一个没人解释得清的 0。
ALLOWED_VERB_KINDS = frozenset(
    {(VERB_DOWNLOAD, KIND_SKILL), (VERB_DOWNLOAD, KIND_AGENT), (VERB_DOWNLOAD, KIND_TOOL)}
    | {(VERB_LIKE, KIND_SKILL), (VERB_LIKE, KIND_AGENT), (VERB_LIKE, KIND_TOOL)}
    | {(VERB_UNLIKE, KIND_SKILL), (VERB_UNLIKE, KIND_AGENT), (VERB_UNLIKE, KIND_TOOL)}
    | {(VERB_OPEN, KIND_AGENT), (VERB_OPEN, KIND_TOOL)}
)

#: 逐条处理结果，返回体里逐条给出。
ACTION_APPLIED = "applied"      # 落库了，业务表也改了
ACTION_DUPLICATE = "duplicate"  # (source, eventId) 已存在，什么都没做
ACTION_SKIPPED = "skipped"      # 形状合法但落不了地（资产不在门户里），带 detail


class RpAiExternalUsageEvent(Base):
    __tablename__ = "rp_ai_external_usage_event"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    #: 哪个上游推来的，见 ``SOURCE_RVC``。
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    #: 对方侧的事件唯一 id。幂等键的另一半。
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)

    #: ``ALLOWED_VERBS`` 之一。
    verb: Mapped[str] = mapped_column(String(16), nullable=False)

    #: ``ALLOWED_KINDS`` 之一。
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    #: **对方自己的** 资产 id，原样存。门户侧的 id 是它加 ``ivc-`` 前缀，那个前缀是我们的
    #: 存储细节，不该写进对方的推送器 —— 前缀哪天改了，那边会静默对不上。
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)

    #: 门户侧解析出来的 id（``ivc-<asset_id>``），解析不到时留空。留着是为了让「这条为什么
    #: skipped」事后查得出来，不必重新推一遍。
    resolved_id: Mapped[str | None] = mapped_column(String(128), index=True)

    #: 使用人工号，大写。
    eid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    #: 该人当时的部门快照。理由同 ``RpAiUsageEvent.department``：部门和部门→组的映射都会变，
    #: 读取时 join 会让一次重组静默改写上个季度的报告。
    department: Mapped[str | None] = mapped_column(String(64))

    #: 事件在**对方那边**发生的时间，已换算成库本地时区的 naive 值（与全仓其余表一致）。
    #: 必须由对方给：用 ``now()`` 会把一整夜的事件全戳在批处理那一刻，而报告是滚动 7 天／
    #: 月窗口，边界上的归属会错。
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    #: 本行落库时间。和 ``occurred_at`` 分开存，两者的差就是推送延迟。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    #: ``ACTION_*`` 之一，以及 skipped 的原因。存下来是因为返回体是一次性的：对方的实现者
    #: 只看 HTTP 状态码（整批 202）时，这里是唯一能回答「那批到底收下了多少」的地方。
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        # 幂等键。按来源分开，见 SOURCE_RVC 的注释。
        UniqueConstraint("source", "event_id", name="uq_ext_usage_source_event"),
        # 「这个资产的点赞/取消历史」——唯一需要按资产回看的查询。
        Index("ix_ext_usage_resolved_occurred", "resolved_id", "occurred_at"),
    )
