"""下载量 = **有多少个不同的人下载过**，不是下载动作发生了多少次。

## 为什么改

线上实测：`outlook-assistant` 的 93 次下载全部来自一个工号 `EYLAIXN`，相邻间隔中位数
**1.0 秒**，92 个间隔里 88 个小于 5 秒 —— 那是一个循环，不是 93 个人。按次数算，一个脚本
就能把任何条目顶到排行榜第一，而榜单是拿来做决策的。

## 为什么这个文件必须存在

改之前，同一个概念在仓里有**两套算法**：

* `services/asset_stats.attach_market_stats` 用 `count(distinct eid)` —— agent 市场卡片
  已经是去重的，前端那行 `title="Unique downloaders"` 就是它。
* 技能卡片读 `rp_ai_skill.downloads` 累计列（`routers/skills._bump_downloads` 每次 +1）、
  盘点与部门页用 `func.count()`、admin 月报也用 `func.count()` —— 全都不去重。

于是同一个人下载同一个技能 10 次，在 agent 市场算 1、在技能市场算 10。这个差别不报错，
只是两个页面对同一件事给出不同的数，而谁都不会想到去对。

## 规则

``count(distinct eid)`` **加上「有没有匿名行」这一项**。

SQL 的 `COUNT(DISTINCT eid)` 会**整体忽略 NULL**，所以只写 `count(distinct eid)` 等于把
匿名下载算成 0 次。线上技能表有 10 行 eid 为空（都在 2026-08-06..12，那时下载端点还不要求
身份），只用 distinct 会让 `tavily-web-search` 少掉 9 次真实下载，而一个真被下载过的东西
显示 0 是明摆着的假话。反过来把 10 行各算一个人也不对：其中 6 行落在同一天的 12 分钟内，
更像一个人在重试。

所以匿名行整体算**一个身份未知的下载者**：认不出来的不假装认得出，也不假装没发生。

## 关于 ``rp_ai_skill.downloads`` 那个累计列

它**不再是显示来源**。线上它是 399，而事件表只有 356 行 —— 差的 43 次是事件表建起来
（最早一条 2026-08-06）之前的下载，那些行**从来没记过是谁下的**，按 eid 去重对它们无定义。
两个来源就会漂移，这 43 就是漂移本身。真相只有事件表一处。
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.models.download import RpAiSkillDownload
from app.models.market_download import RpAiMarketDownload

# (事件表, 条目 id 列) —— 两个市场，同一条规则。
SKILL = (RpAiSkillDownload, RpAiSkillDownload.skill_id)
MARKET = (RpAiMarketDownload, RpAiMarketDownload.item_id)


def _window(table, since: datetime | None, until: datetime | None) -> list:
    conds = []
    if since is not None:
        conds.append(table.created_at >= since)
    if until is not None:
        conds.append(table.created_at < until)
    return conds


def unique_downloads(
    db: Session,
    kind: tuple,
    ids: Iterable[str] | None = None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, int]:
    """每个条目有多少个**不同的**下载者。

    ``kind`` 传 ``SKILL`` 或 ``MARKET``。``ids`` 为 None 时统计全部条目（盘点用），给了就
    只统计这些（列表页用，避免把整张表拉进来）。``since``/``until`` 给报告按周期统计用 ——
    **去重是在窗口内做的**：同一个人一月下一次、二月又下一次，两个月各算一次，因为月报
    问的是"这个月有多少人拿了它"。

    返回的 dict 只包含有下载记录的条目，没有的取 0（调用方用 ``.get(id, 0)``）。
    """
    table, id_col = kind
    conds = _window(table, since, until)
    if ids is not None:
        ids = list(ids)
        if not ids:
            return {}
        conds.append(id_col.in_(ids))

    named = dict(
        db.execute(
            select(id_col, func.count(distinct(table.eid)))
            .where(*conds, table.eid.isnot(None))
            .group_by(id_col)
        ).all()
    )
    # 匿名行：每个条目最多贴 1。用单独一次查询而不是 SQL 里拼 CASE —— 两个方言
    # (SQLite 测试 / 生产库) 对 NULL 在聚合里的处理差异是这类 bug 的常见来源，
    # 而这一步的代价是一次走索引的 group by。
    anon = {
        str(r[0])
        for r in db.execute(
            select(id_col)
            .where(*conds, table.eid.is_(None))
            .group_by(id_col)
        ).all()
    }
    out = {str(k): int(v or 0) for k, v in named.items()}
    for k in anon:
        out[k] = out.get(k, 0) + 1
    return out


def unique_downloads_by_department(
    db: Session,
    kind: tuple,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str | None, int]:
    """去重后的下载按**部门快照**分组。给月报的 Group activity 用。

    顺序很要紧：**先按 (条目, 人) 去重，再归部门**。反过来（先按部门数行数、再去重）会让
    同一个人在两个部门各算一次 —— ``department`` 是下载当时抄在行上的快照，一个人换了组之后
    再下载同一个东西，两行的部门不同，于是他一个人被算进两个组，两个组的数都变大，而合计
    也跟着变大、和总下载量对不上。

    代表行取 ``min(id)``，即这个人**第一次**下载时所属的部门。取最后一次也说得通，但必须
    定死一个，否则同一份报告重跑两次可能给出不同的分组 —— 而一份不可复现的报告比一份有偏差
    的报告更难用。
    """
    table, id_col = kind
    conds = _window(table, since, until)
    # 子查询：每个 (条目, 人) 只留一行。GROUP BY 会把 NULL 的 eid 归成一组，所以匿名行
    # 也自然按"每个条目一个未知下载者"折叠，和 unique_downloads 的规则一致。
    reps = (
        select(func.min(table.id).label("rid")).where(*conds).group_by(id_col, table.eid)
    ).subquery()
    rows = db.execute(
        select(table.department, func.count())
        .join(reps, reps.c.rid == table.id)
        .group_by(table.department)
    ).all()
    return {r[0]: int(r[1] or 0) for r in rows}


def unique_downloads_total(
    db: Session,
    kind: tuple,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> int:
    """全站合计：**不同的 (条目, 人) 组合**有多少。

    刻意不是"有多少个不同的人"：一个人下了 5 个不同的技能，报告要的是 5,因为那是 5 次
    真实的取用。也刻意不是 ``sum(unique_downloads().values())`` 之外的另一套算法 —— 它
    与逐条目的口径必须一致,否则总数和明细对不上,而这种对不上没人能一眼看出是哪边错。
    """
    return sum(unique_downloads(db, kind, since=since, until=until).values())
