"""把 manifest 里的 ``owner`` 一行字解析成一个人。

为什么需要一整个模块:``radio/ai-assets`` 的 19 个 ``manifest.yaml`` 里,``owner`` 实测有
六种形态,而门户需要的是两样东西 —— 一个工号(``creator``,报表按它归部门)和一个显示名
(``author``,卡片上给人看的)。实测的六种:

===========================  =========================  ===================
manifest 里的原文             解析依据                    结果
===========================  =========================  ===================
``ecuiden``                  当工号查                    ECUIDEN
``Adam Eriksson (eeriada)``  括号里就是工号              EERIADA
``shaolei.zhang``            当邮箱前缀查                EHSHAZA
``rui.li.ext@ericsson.com``  当邮箱查                    ZLXRUII
``Baimin Xu``                当显示名查                  EBAIMXU
``Yijian Yan, Boyu Liu``     逗号分隔,取第一个           ZYIJYAN
``radio-ai-platform``        团队名,目录里没有这个人     兜底
===========================  =========================  ===================

顺序是有讲究的:工号那一条必须最先,因为一个纯小写无空格的串既可能是工号也可能是别的什么,
而工号是唯一能直接确认的形态。括号那一条排在姓名之前,因为 ``Adam Eriksson (eeriada)`` 按
显示名查是查不到的(目录里的显示名不带括号)。

**为什么不接受"手填部门"这类输入**:那会让报表里出现一个没法追溯来源的数字。这里只认目录
的回答,查不到就说查不到、走兜底,而兜底是显式记录下来的。
"""

from __future__ import annotations

import logging
import re
import ssl
from dataclasses import dataclass

from ldap3 import ALL, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException

from app.config import get_settings

log = logging.getLogger(__name__)

#: ``Adam Eriksson (eeriada)`` —— 括号里那一串。
_PARENS = re.compile(r"\(([^)]+)\)\s*$")

#: LDAP 过滤器里有特殊含义的字符。不转义的话,一个名字里的括号就能改变过滤器的结构。
_ESCAPES = {"\\": r"\5c", "(": r"\28", ")": r"\29", "*": r"\2a", "\0": r"\00"}


def _esc(value: str) -> str:
    out = value
    for raw, escaped in _ESCAPES.items():
        out = out.replace(raw, escaped)
    return out


@dataclass(frozen=True)
class ResolvedOwner:
    """一次解析的完整结果,包含"怎么得出来的"。

    ``how`` 不是日志装饰:同步是无人值守的,当某个资产归到了意外的人名下时,唯一能回答
    "为什么"的就是这一行。它会进同步的返回体里。
    """

    eid: str
    display_name: str
    department: str | None
    how: str
    #: 目录里查不到、用了配置里那个兜底工号。报表会把这个资产算成兜底那个人的产出,所以
    #: 调用方要能把这些挑出来报告,而不是让它悄悄混进去。
    fell_back: bool = False


class _Directory:
    """一次同步期间共用一条 LDAP 连接,并记住查过的值。

    19 个资产里 ``ecuiden`` 出现 5 次、``shaolei.zhang`` 出现 2 次。每次都重新查一遍是在
    为同一个答案付网络往返,而一次同步里目录不会变。
    """

    def __init__(self) -> None:
        self._conn: Connection | None = None
        self._cache: dict[str, list[tuple[str, str, str]]] = {}

    def _connect(self) -> Connection:
        if self._conn is not None:
            return self._conn
        settings = get_settings()
        server = Server(
            settings.ldap_url,
            use_ssl=settings.ldap_url.lower().startswith("ldaps"),
            tls=Tls(validate=ssl.CERT_NONE) if not settings.ldap_verify_tls else None,
            get_info=ALL,
        )
        self._conn = Connection(
            server,
            user=settings.ldap_bind_user,
            password=settings.ldap_bind_password,
            auto_bind=True,
            receive_timeout=settings.ldap_timeout,
        )
        return self._conn

    def search(self, flt: str) -> list[tuple[str, str, str]]:
        """``[(signum, displayName, department)]``,失败一律当作查不到。

        目录出问题不该让整次同步失败:一个解析不出来的 owner 走兜底,而目录连不上会让**所有**
        owner 走兜底 —— 那是个明显的、可见的结果,比抛异常中断同步更容易发现,也不会让上一次
        同步的成果半途而废。
        """
        if flt in self._cache:
            return self._cache[flt]
        try:
            settings = get_settings()
            conn = self._connect()
            ok = conn.search(
                settings.ldap_base_dn,
                flt,
                search_scope=SUBTREE,
                attributes=["sAMAccountName", "displayName", "department"],
            )
            rows = (
                [
                    (
                        str(e.sAMAccountName) if "sAMAccountName" in e else "",
                        str(e.displayName) if "displayName" in e else "",
                        str(e.department) if "department" in e else "",
                    )
                    for e in conn.entries
                ]
                if ok
                else []
            )
        except LDAPException as exc:
            log.warning("LDAP 查询失败(%s),这个 owner 会走兜底: %s", flt, exc)
            rows = []
        # department 在目录里可能是 `[]`(外部人员就是这样),那和空一样。
        rows = [(a, b, "" if c in ("[]", "None") else c) for a, b, c in rows if a]
        self._cache[flt] = rows
        return rows

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.unbind()
            except LDAPException:
                pass
            self._conn = None


def _candidates(owner: str) -> list[tuple[str, str]]:
    """``[(怎么查的, LDAP 过滤器)]``,按可信度从高到低。"""
    value = owner.strip()
    out: list[tuple[str, str]] = []
    if not value:
        return out

    # 逗号分隔的多人:只有第一个进门户。第二个人在门户里看不见 —— 这是取舍不是遗漏,
    # 一个 creator 只能是一个工号,而把两个人拼进 author 会让"作者"这一列不再是一个人。
    first = value.split(",")[0].strip() or value

    m = _PARENS.search(first)
    if m:
        out.append(("括号里的工号", f"(sAMAccountName={_esc(m.group(1).strip())})"))
    if "@" in first:
        e = _esc(first)
        out.append(("邮箱", f"(|(mail={e})(userPrincipalName={e})(proxyAddresses=smtp:{e}))"))
    elif "." in first and " " not in first:
        # `shaolei.zhang` —— 邮箱前缀。实测这么写的两个资产都能命中。
        out.append(("firstname.lastname 当邮箱前缀", f"(mail={_esc(first)}@*)"))
    if " " not in first and "@" not in first and "." not in first:
        out.append(("当工号", f"(sAMAccountName={_esc(first)})"))
    if " " in first and "(" not in first:
        out.append(("按显示名", f"(displayName={_esc(first)})"))
        # 目录里有人显示名带后缀(`Chenyu Fan A`、`Richard Qiu (EXT)`),精确匹配会漏。
        out.append(("按显示名前缀", f"(displayName={_esc(first)}*)"))
    return out


class DirectoryDepartments:
    """已知工号，只补部门。一次同步用一个实例，共用 LDAP 连接与缓存。

    为什么不用 ``OwnerResolver``：它在「目录里有这个人但他没有部门」时会**换一个工号**
    （``_fallback``），把资产记到配置里那个兜底的人名下。对 Gerrit 那条路径这是对的，因为
    那里连工号本身都是从 ``owner`` 这种自由文本里猜出来的，猜不准就该有人兜着。

    这里不一样：工号来自上游索引，是权威的。查不到部门时正确的答案是**留空**，而不是把
    IVC 的技能记到别人头上 —— 那会让报告里多一个人凭空产出了别人的作品，而且不报错。

    所以这个类刻意只回答一个问题，没有兜底：部门有就给，没有就 ``None``。
    """

    def __init__(self, directory: _Directory | None = None) -> None:
        self._dir = directory or _Directory()

    def department_of(self, eid: str | None) -> str | None:
        """某个工号在目录里的 ``department``，查不到或为空时 ``None``。"""
        value = (eid or "").strip()
        if not value:
            return None
        rows = self._dir.search(f"(sAMAccountName={_esc(value)})")
        if not rows:
            return None
        return rows[0][2].strip() or None

    def close(self) -> None:
        self._dir.close()


class OwnerResolver:
    """一次同步用一个实例:共用 LDAP 连接与缓存。"""

    def __init__(self, directory: _Directory | None = None) -> None:
        self._dir = directory or _Directory()

    def resolve(self, owner: str | None) -> ResolvedOwner:
        settings = get_settings()
        fallback = (settings.gerrit_owner_fallback_eid or "").strip().upper()
        raw = (owner or "").strip()

        for how, flt in _candidates(raw):
            rows = self._dir.search(flt)
            if not rows:
                continue
            eid, display, dept = rows[0]
            if len(rows) > 1:
                # 说出来而不是静默取第一条:一个含糊的 owner 可能把资产记到同名的另一个人头上。
                log.warning(
                    "owner %r 按%s匹配到 %d 个人,取了 %s。", raw, how, len(rows), eid
                )
            if not (dept or "").strip():
                # 目录里有这个人,但他没有部门 —— 实测是外部人员(显示名带 (EXT))。
                # 部门是唯一能推出分组的东西,没有部门就没有归属,资产会计入 "All" 却不出现在
                # 任何部门标签下。所以记到兜底工号名下,和"目录里根本查不到"走同一条路:
                # 这两种情况对门户的意义相同 —— **没有一个能负责的内部人**。
                #
                # 代价说清楚:``author`` 会变成兜底那个人的名字,原作者的署名不再显示。
                # 这是有意的取舍(有人负责 > 署名准确),不是漏掉。
                return self._fallback(
                    fallback, f"{raw!r} 在目录里没有部门,记到 {fallback} 名下"
                )
            return ResolvedOwner(
                eid=eid.strip().upper(),
                display_name=display.strip() or eid.strip().upper(),
                department=dept.strip(),
                how=how,
            )

        # 走到这里说明目录里没有这个 owner:团队名、离职的人、或者名字拼错了。
        return self._fallback(fallback, f"目录里查不到 {raw!r},兜底到 {fallback}")

    def _fallback(self, fallback: str, how: str) -> ResolvedOwner:
        """把资产记到兜底工号名下,连带取它的部门(分组是从部门推的)。"""
        rows = self._dir.search(f"(sAMAccountName={_esc(fallback)})") if fallback else []
        display, dept = (rows[0][1], rows[0][2]) if rows else ("", "")
        return ResolvedOwner(
            eid=fallback,
            display_name=display.strip() or fallback,
            department=dept.strip() or None,
            how=how,
            fell_back=True,
        )

    def close(self) -> None:
        self._dir.close()
