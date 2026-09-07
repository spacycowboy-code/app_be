"""IVC 同步补部门 —— 归属链上唯一那一环。

为什么值得单独一个文件：漏写 ``department`` 这一列不报错。同步照常成功、技能照常出现在
市场里、卡片照常显示作者，只有报告和部门页会把这些资产算进「无法归属」。实测代价是 5 个
真人、10 件 IVC 技能，而这 5 个人在目录里**都有** IVC 的部门 —— 数据一直拿得到。

所以这里钉的是三件都不会报错的事：

* 建新行时写了部门；
* 已有行**空的**部门被补上（这是历史欠的那 5 行自动修复的机制，不需要另写回填脚本）；
* 已有行**非空**的部门不被覆盖 —— 那可能是这个人自己登录时写下的、比目录快照新。

外加一条：部门查不到时**不许**换一个人。这是 ``DirectoryDepartments`` 与
``OwnerResolver`` 的区别所在，也是唯一一处「修错了比不修更糟」的地方。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.user import RpAiPortal
from app.routers.ivc_sync import _ensured_user
from app.services.asset_owner import DirectoryDepartments

IVC_DEPT = "BNEW RTE RP IVC TE Test Environment"
EIDS = ("ETIVC001", "ETIVC002", "ETIVC003", "ETIVC004")


@pytest.fixture()
def db():
    with SessionLocal() as session:
        yield session
        for eid in EIDS:
            row = session.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))
            if row is not None:
                session.delete(row)
        session.commit()


def _row(session, eid: str) -> RpAiPortal | None:
    return session.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))


def test_a_new_row_gets_the_department(db) -> None:
    _ensured_user(db, "ETIVC001", "IVC Person", IVC_DEPT)
    db.commit()
    row = _row(db, "ETIVC001")
    assert row is not None
    assert row.department == IVC_DEPT
    # origin 仍必须是 import:这些人没登录过,算进「曾登录过的人数」会虚增渗透率。
    assert row.origin == "import"


def test_an_existing_row_with_no_department_is_backfilled(db) -> None:
    """历史欠的那 5 行就是这个形状,所以跑一次同步即自动修好。"""
    db.add(RpAiPortal(eid="ETIVC002", display_name="Old Row", origin="import"))
    db.commit()
    assert _row(db, "ETIVC002").department is None

    _ensured_user(db, "ETIVC002", "Old Row", IVC_DEPT)
    db.commit()
    assert _row(db, "ETIVC002").department == IVC_DEPT


def test_an_existing_department_is_not_overwritten(db) -> None:
    """登录写下的值比目录快照新,同步不该把它盖回去。

    也顺带钉住 origin 不被降级:一条真登录建的行被导入改成 ``import``,会把一个真实用户
    从渗透率里抹掉。
    """
    db.add(
        RpAiPortal(
            eid="ETIVC003",
            display_name="Signed In",
            department="BNEW RTE RP PS AI AI & Innovation",
            origin="login",
        )
    )
    db.commit()

    _ensured_user(db, "ETIVC003", "Signed In", IVC_DEPT)
    db.commit()
    row = _row(db, "ETIVC003")
    assert row.department == "BNEW RTE RP PS AI AI & Innovation"
    assert row.origin == "login"


def test_no_department_available_leaves_it_empty_rather_than_inventing_one(db) -> None:
    """查不到就留空。留空是显式的空白,填一个错的部门是一个查不出来的错。"""
    _ensured_user(db, "ETIVC004", "No Dept", None)
    db.commit()
    row = _row(db, "ETIVC004")
    assert row is not None
    assert row.department is None


class _StubDirectory:
    """按工号返回 ``(eid, display, department)`` 的假目录。"""

    def __init__(self, rows: dict[str, tuple[str, str, str]]) -> None:
        self._rows = rows
        self.searched: list[str] = []

    def search(self, flt: str) -> list[tuple[str, str, str]]:
        self.searched.append(flt)
        for eid, row in self._rows.items():
            if f"sAMAccountName={eid}" in flt:
                return [row]
        return []

    def close(self) -> None:
        return None


def test_the_lookup_answers_with_a_department_or_nothing() -> None:
    """查到人但他没有部门时给 ``None``,**不换成另一个人**。

    这正是不复用 ``OwnerResolver`` 的原因:它在这种情况下会 ``_fallback`` 到配置里那个兜底
    工号,于是 IVC 的技能被记到别人名下 —— 报告里会出现一个人凭空产出了别人的作品,而且
    不报错。这里工号来自上游索引、是权威的,只缺部门。
    """
    stub = _StubDirectory(
        {
            "EHASDEPT": ("ehasdept", "Has Dept", IVC_DEPT),
            "ENODEPT": ("enodept", "No Dept", "   "),
        }
    )
    lookup = DirectoryDepartments(stub)  # type: ignore[arg-type]

    assert lookup.department_of("EHASDEPT") == IVC_DEPT
    assert lookup.department_of("ENODEPT") is None
    assert lookup.department_of("ENOTINDIR") is None
    assert lookup.department_of(None) is None
    assert lookup.department_of("  ") is None
    # 空值不该白跑一次 LDAP 查询。
    assert len(stub.searched) == 3
