"""建 ``rp_ai_portal`` 行的唯一入口 —— 并且一定带上 department。

**为什么要收成一处。** 这个仓里原本有四份几乎一样的 `_ensure_user`(技能上传、包上传、
收藏、IVC 同步),其中三份只写 eid、不写 department。而报告与 Department Details 两条路径
都按 department 归属,列是空的就等于这个人不属于任何部门,他名下的资产落进「无法归属」。

这个缺口不报错：行建出来了、上传成功了、卡片正常显示、只是那个人从部门统计里消失了。
2026-09-07 修 `ivc_sync` 那一处时代价已经量过：5 个真人、10 件 IVC 技能。剩下三处是同一个
形状,所以这里不是再补一处,而是让"建行"只有一种写法。

**为什么查目录不会拖慢热路径。** 只在"这个人还没有行"这个分支里查一次。走到那个分支意味着
此人从未登录过门户(登录会建行),对收藏/上传这些界面路径几乎不可能发生 —— 它是直连接口
才会走到的兜底。而且查失败不阻塞：拿不到部门就留空并照常建行,因为拦下一次上传的代价远大
于一行少一列。

**为什么留空而不是猜。** 目录里查不到部门时正确答案是空。编一个值会在报告里变成一个事后
没人能判断对错的数字 —— 这正是 `tools/register_directory_users.py` 拒绝提供 `--department`
手填参数的同一条理由。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import RpAiPortal

logger = logging.getLogger(__name__)

DEPARTMENT_MAX = 64


def _lookup_department(eid: str) -> str | None:
    """从目录读这个人的部门。查不到、或目录不可用时返回 ``None``。

    **刻意不用** ``OwnerResolver.resolve()``：它在"目录里有这个人但他没有部门"时会
    ``_fallback`` 换成配置里那个兜底工号。对 Gerrit 那条路径这是对的(那里工号本身就是从
    自由文本里猜的),但这里工号是权威的,把资产记到别人名下会让报告里出现一个人凭空产出了
    别人的作品,而且不报错。
    """
    try:
        # 局部 import：这个模块被上传路径 import，而 asset_owner 会拉起 ldap3。
        from app.services.asset_owner import DirectoryDepartments

        directory = DirectoryDepartments()
        try:
            return directory.department_of(eid)
        finally:
            directory.close()
    except Exception:  # noqa: BLE001
        # 目录不可用不该拦下一次上传或一次收藏。
        logger.warning("查目录失败，%s 的部门留空", eid, exc_info=True)
        return None


def ensure_portal_user(
    db: Session,
    eid: str,
    *,
    display_name: str | None = None,
    department: str | None = None,
    origin: str | None = None,
    lookup_directory: bool = True,
    promote_display_name: bool = False,
) -> RpAiPortal:
    """确保 ``eid`` 有一行，并尽力让它带上 department。

    ``department`` 由调用方给时优先用它(同步路径手上已经有,不必再查一次目录)。没给且
    ``lookup_directory`` 为真时查目录。

    已有行**只补空缺、不覆盖**：非空的值可能是这个人自己登录时写下的，比目录快照新。
    而"只补空缺"正是让历史欠的那些行在下一次经过时自动补齐的机制。

    ``promote_display_name`` 给同步路径用：那里 ``display_name`` 可能已经是工号本身(早先建行
    时没有人名可写),上游后来给了真名就该换上去。默认关，因为对交互路径来说"已经有名字了就
    别动"才是对的。

    ``origin`` 只在**建行**时写，已有行一律不动：它可能是真的登录建的，一次导入不该把它降级
    成 import，那会让报告里「曾登录过的人数」少算一个人。
    """
    eid = (eid or "").strip()
    if not eid:
        raise ValueError("eid 不能为空")

    row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))
    dept = (department or "").strip() or None
    if dept is None and lookup_directory:
        # strip 必须在这里也做一次。只对调用方传进来的值做，会让目录返回的空白串
        # （"   "）被当成有效部门写进库 —— 那一行看起来有值，报告里却归不出任何部门，
        # 而且 `not row.department` 从此为假，以后再也不会被补上。测试钉住这一条。
        dept = (_lookup_department(eid) or "").strip() or None

    if row is None:
        row = RpAiPortal(
            eid=eid,
            # **不拿工号当人名。** 这里曾经写 `display_name or eid`，结果没有人名的行被填成
            # 工号，而下游把「有 display_name」当成「知道这个人叫什么」，于是审核 MR 的标题
            # 印成 `TESTER (TESTER)`。留 None 才是诚实的「还不知道」，卡片与 MR 标题各自的
            # 兜底逻辑本来就处理得了。全量套件里 test_the_review_mr_names_the_uploader 钉住它。
            display_name=display_name,
            department=dept[:DEPARTMENT_MAX] if dept else None,
        )
        if origin is not None:
            row.origin = origin
        db.add(row)
        db.flush()
        return row

    if not row.department and dept:
        row.department = dept[:DEPARTMENT_MAX]
    if display_name and (
        not row.display_name or (promote_display_name and row.display_name == eid)
    ):
        row.display_name = display_name
    return row
