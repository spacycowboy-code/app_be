"""解析 IVC 的 Nexus 索引，并在写库之前把它检查一遍。

**为什么是粘贴而不是抓取。** 索引在
``arm1s10-eiffel239.eiffel.gic.ericsson.se:8443``，实测生产宿主机到那台机器 ICMP 通（0% 丢包）
而 **TCP 22 / 80 / 443 / 8081 / 8443 全部静默超时**，且容器与宿主机都没有代理配置。所以门户
自己去拉这条路在防火墙放行之前不存在。能访问它的是员工的工位浏览器，于是入口改成由白名单
里的人把 JSON 贴进来。

**这一层只做解析和体检，不碰数据库**，因为要先回答一个它无法自己回答的问题：索引里的
``download_url`` 指向那台不可达的 Nexus，所以门户拿不到包体，也就无法把包推进 GitLab。目录
（名字、描述、作者、版本、标签、链接）是拿得到的，包体不是。把两者混在一个函数里，会让
"能同步了"这句话同时意味着两件真假不同的事。

体检要挡的是四件从真实样本里看出来的事：

* ``created_at`` 18 条全等（1788170082 = 2026-08-31 09:54:42 UTC），它是索引生成时间而非资产
  创建时间。当成创建日期写进库，"本月新增资产"会一次虚增 18。
* skill 的身份是 ``author``（工号，大小写混用：16 个小写 + ``EPENYAN`` 大写），agent 换成
  ``owner``（``wenjie.a.liu``，不是工号也没有 display_name）。门户内部工号统一大写。
* 两类的字段结构不同：skill 是平铺的 ``download_url``，agent 藏在 ``runtimes[].download_url``
  里并按 runtime 分。
* 下载地址必须限定在已知主机上。它来自一次人工粘贴，而"信一个用户给的 URL"正是市场下载
  端点刻意拒绝手工登记条目的原因。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

#: 唯一允许的下载来源。粘贴进来的 URL 指向别处时，那一条不入库 —— 一个能被替换成任意地址的
#: 下载按钮，比少同步一个资产危险得多。
ALLOWED_DOWNLOAD_HOST = "arm1s10-eiffel239.eiffel.gic.ericsson.se"

#: 索引里 agent 的 ``owner`` 是 ``wenjie.a.liu`` 这种用户名形态，报告只认工号。映射不出来时
#: 留空并在结果里说明，不猜一个人 —— 月报的「数字资产」一列按工号关联组织树，填错等于把
#: 资产记到别人部门下。
OWNER_TO_EID: dict[str, str] = {}


class IndexRejected(ValueError):
    """整份索引不可用（不是某一条有问题）。"""


@dataclass
class Entry:
    """一条待同步的资产，字段已归一化。"""

    kind: str  # "skill" | "agent"
    id: str
    name: str
    version: str
    description: str
    tags: list[str]
    download_url: str
    #: 上游给的身份原文，保留是为了在结果里说清映射依据。
    raw_owner: str
    #: 归一化后的工号，映射不出来时为 None。
    eid: str | None
    #: 上游给的人名，只有 skill 有。
    display_name: str | None = None
    skill_md_url: str | None = None
    runtime: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return not self.problems


@dataclass
class ParsedIndex:
    entries: list[Entry]
    #: 索引自报的生成时间，明确不是资产创建时间。
    generated_at: datetime | None
    #: 整份索引层面的观察，原样呈现给操作者。
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> list[Entry]:
        return [e for e in self.entries if e.usable]

    @property
    def rejected(self) -> list[Entry]:
        return [e for e in self.entries if not e.usable]


def _norm_eid(raw: str | None) -> str | None:
    """工号归一化成大写。样本里 16 个小写、``EPENYAN`` 大写，不归一化同一个人会被算两次。"""
    if not raw:
        return None
    value = raw.strip().upper()
    return value or None


def _looks_like_eid(value: str | None) -> bool:
    """Ericsson 工号形态：E + 6 位字母数字。

    刻意只做形态判断，不查库 —— 8 位 skill 作者里有 5 个从未登录门户、用户表里没有他们的行，
    但他们都在 950 人组织树里，所以"不在用户表"不该成为拒收的理由。
    """
    return bool(value) and len(value) == 7 and value[0] == "E" and value[1:].isalnum()


def _check_url(url: str | None, problems: list[str]) -> str:
    if not url:
        problems.append("没有下载地址")
        return ""
    host = (urlparse(url).hostname or "").lower()
    if host != ALLOWED_DOWNLOAD_HOST:
        problems.append(f"下载地址不在允许的主机上：{host or '无法解析'}")
    return url


def _entry_from_skill(raw: dict) -> Entry:
    problems: list[str] = []
    eid = _norm_eid(raw.get("author"))
    if not eid:
        problems.append("没有 author")
    elif not _looks_like_eid(eid):
        problems.append(f"author 不是工号形态：{eid}")
    return Entry(
        kind="skill",
        id=(raw.get("id") or "").strip(),
        name=(raw.get("name") or raw.get("id") or "").strip(),
        version=(raw.get("version") or "").strip(),
        description=(raw.get("description") or "").strip(),
        tags=[t for t in (raw.get("tags") or []) if t],
        download_url=_check_url(raw.get("download_url"), problems),
        raw_owner=(raw.get("author") or "").strip(),
        eid=eid if _looks_like_eid(eid) else None,
        display_name=(raw.get("display_name") or "").strip() or None,
        skill_md_url=(raw.get("skill_md_url") or "").strip() or None,
        problems=problems,
    )


def _entry_from_agent(raw: dict) -> Entry:
    problems: list[str] = []
    owner = (raw.get("owner") or "").strip()
    eid = _norm_eid(OWNER_TO_EID.get(owner)) if owner else None
    if not owner:
        problems.append("没有 owner")
    elif eid is None:
        # 不是错误，是缺一条映射：条目仍可入库，只是归不到部门。
        problems.append(f"owner「{owner}」还没有对应的工号，需要补一条映射")

    runtimes = raw.get("runtimes") or []
    if not runtimes:
        problems.append("runtimes 是空的，没有可下载的包")
        url, runtime = "", None
    else:
        # 只有 kiro 一种 runtime 时取它；将来多种时取第一个并在结果里说明。
        chosen = next((r for r in runtimes if r.get("runtime") == "kiro"), runtimes[0])
        runtime = (chosen.get("runtime") or "").strip() or None
        url = _check_url(chosen.get("download_url"), problems)
        if len(runtimes) > 1:
            problems.append(
                f"有 {len(runtimes)} 个 runtime，只取了 {runtime}，其余需要单独决定"
            )

    if raw.get("deprecated"):
        problems.append("上游标记为 deprecated")

    return Entry(
        kind="agent",
        id=(raw.get("id") or "").strip(),
        name=(raw.get("name") or raw.get("id") or "").strip(),
        version=(raw.get("version") or "").strip(),
        description=(raw.get("description") or "").strip(),
        tags=[t for t in (raw.get("tags") or []) if t],
        download_url=url,
        raw_owner=owner,
        eid=eid,
        runtime=runtime,
        problems=problems,
    )


def parse(payload: object) -> ParsedIndex:
    """把粘贴进来的索引变成一串归一化的条目，附带整份索引层面的观察。

    不抛异常挡掉整份索引，除非它连形状都不对 —— 一条有问题时报告那一条，让操作者看到其余
    17 条仍然可用，比整批拒收有用。
    """
    if not isinstance(payload, dict):
        raise IndexRejected("索引的最外层应该是一个对象，含 skills 与 agents 两个键")
    skills = payload.get("skills")
    agents = payload.get("agents")
    if skills is None and agents is None:
        raise IndexRejected("索引里既没有 skills 也没有 agents")
    for name, value in (("skills", skills), ("agents", agents)):
        if value is not None and not isinstance(value, list):
            raise IndexRejected(f"{name} 应该是一个数组")

    entries: list[Entry] = []
    for raw in skills or []:
        entries.append(_entry_from_skill(raw))
    for raw in agents or []:
        entries.append(_entry_from_agent(raw))

    if not entries:
        raise IndexRejected("索引里一条资产都没有")

    for entry in entries:
        if not entry.id:
            entry.problems.append("没有 id")
        if not entry.version:
            entry.problems.append("没有版本号")

    notes: list[str] = []

    # created_at：全等说明它是索引生成时间。这一句必须出现在操作者眼前，因为把它当创建日期
    # 用是最容易犯且最难发现的错 —— 数字看起来完全正常，只是全落在同一天。
    stamps = {
        str(raw.get("created_at"))
        for raw in list(skills or []) + list(agents or [])
        if raw.get("created_at")
    }
    generated_at = None
    if len(stamps) == 1:
        only = stamps.pop()
        try:
            generated_at = datetime.fromtimestamp(int(only), timezone.utc)
            notes.append(
                f"全部 {len(entries)} 条的 created_at 都是 {only}"
                f"（{generated_at:%Y-%m-%d %H:%M:%S} UTC）：这是索引生成时间，"
                "不是资产创建时间，不会被当作创建日期写入。"
            )
        except (TypeError, ValueError):
            notes.append(f"created_at 不是时间戳：{only}")
    elif len(stamps) > 1:
        notes.append(f"created_at 有 {len(stamps)} 个不同的值，可能已经是真实创建时间了。")

    # 同一份索引里的重复 id：上游不该有，但重复入库会让后一条静默覆盖前一条。
    seen: dict[tuple[str, str], int] = {}
    for entry in entries:
        key = (entry.kind, entry.id)
        seen[key] = seen.get(key, 0) + 1
    for (kind, ident), count in seen.items():
        if count > 1:
            notes.append(f"索引里有 {count} 条重复的 {kind} id「{ident}」")

    return ParsedIndex(entries=entries, generated_at=generated_at, notes=notes)
