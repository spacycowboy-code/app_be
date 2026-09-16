"""谁能编辑 Radio SW 的 R&D Guide。

**两条来源，任一成立即可：**

1. ``rp_ai_rdg_admin`` 表里显式加的人 —— RSW 自己维护的名单，由他们在 Admin -> RSW 上增删。
2. **门户 admin 名单**（``settings.rsw_page_eid_list``,即 dashboard 名单 + RSW 增量）。

第 2 条是 2026-09-16 加的,Miao 的原话是「那个 rsw 的所有页面 我们的 admin 组应该有权限」。在那之前
门户 admin 里只有 EZHGMIO 恰好也在表里,于是 ADMINMM / EHUIWUU / EBAOXUE 能进 Admin -> RSW 这一页、
却编辑不了 R&D Guide,而且页面上那块「R&D Guide 编辑权限」对他们直接隐藏 —— 一个进得去但什么都做不了
的页面。

**为什么用 ``rsw_page_eid_list`` 而不是 ``dashboard_eid_list``。** 前者正是「谁能看 Admin -> RSW
这一页」的名单,所以这条规则读起来是一句话:能看这一页的人就能编辑这一页管的东西。用 dashboard 名单
会把 ECUIDEN 排除在外,而她本来就在表里,规则会变得比现实更窄、也更难解释。

**为什么不往表里插三行了事。** 那是一次性数据改动:下一个进 admin 组的人还得再插一次,而且插了之后
「他为什么能编辑」这件事只存在于数据里、代码里读不出来。规则写在代码里,数据里那张表就只承担它真正
的职责 —— RSW 自己额外授权给谁。

**代价:门户 admin 不能在界面上被移除。** 他们不是表里的行,``DELETE /admins/{eid}`` 对他们没有可删
的东西。所以 ``effective_admins`` 把来源标出来,界面只对显式行显示移除按钮 —— 一个点了没反应或者报
404 的按钮比没有按钮更坏。要收走门户 admin 的这项权限,就是把他从 admin 名单里去掉。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.rdg import RpAiRdgAdmin

#: 权限的来源。``list`` = ``rp_ai_rdg_admin`` 里显式加的,可以在界面上移除;
#: ``portal-admin`` = 因为在门户 admin 名单里而拥有,界面上不可移除。
SOURCE_LIST = "list"
SOURCE_PORTAL_ADMIN = "portal-admin"


def _portal_admins() -> list[str]:
    return [e.upper() for e in get_settings().rsw_page_eid_list]


def explicit_admins(db: Session) -> list[str]:
    """``rp_ai_rdg_admin`` 表里的人,按工号排序。"""
    return sorted({r.eid.upper() for r in db.scalars(select(RpAiRdgAdmin)).all()})


def is_rsw_admin(db: Session, eid: str) -> bool:
    """这个人能不能编辑 R&D Guide。

    ``/api/auth/me`` 的 ``isRswAdmin`` 与 ``rdg`` 路由的闸门**都走这里**。两处各写一遍判断的话,
    界面会显示一个按钮而后端拒绝它,或者反过来 —— 而两者都不报错,只是不一致。
    """
    who = (eid or "").strip().upper()
    if not who:
        return False
    return who in _portal_admins() or who in explicit_admins(db)


def effective_admins(db: Session) -> list[dict]:
    """现在实际能编辑 R&D Guide 的所有人,带来源。

    显式行排在前面 —— 那是这一页真正在管的东西;门户 admin 是规则派生的,放后面。同一个人两种来源都
    命中时算显式(``SOURCE_LIST``),因为那一行确实存在、也确实能删。
    """
    explicit = explicit_admins(db)
    out = [{"eid": e, "source": SOURCE_LIST} for e in explicit]
    out += [
        {"eid": e, "source": SOURCE_PORTAL_ADMIN}
        for e in sorted(set(_portal_admins()) - set(explicit))
    ]
    return out
