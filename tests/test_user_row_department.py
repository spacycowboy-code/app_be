"""建 ``rp_ai_portal`` 行时一定带上 department。

这里钉的是一个**不报错**的缺口：行建出来了、上传/收藏成功了、卡片正常显示，只是那个人
department 为空，于是报告与 Department Details 都无法归属他，他名下的资产落进「无法归属」。
2026-09-07 在 ``ivc_sync`` 修过同一个形状，代价量过：5 个真人、10 件 IVC 技能。

那次只修了一处。这个文件覆盖剩下三处（技能上传、包上传、收藏）以及共享入口本身。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import delete, select

from app.database import SessionLocal
from app.models.user import RpAiPortal
from app.services.portal_users import ensure_portal_user

EID = "EPROBE1"
OTHER = "EPROBE2"
DEPT = "BNEW RTE RP IVC T4 Team 4"


@pytest.fixture(autouse=True)
def _clean():
    with SessionLocal() as db:
        db.execute(delete(RpAiPortal).where(RpAiPortal.eid.in_([EID, OTHER])))
        db.commit()
    yield
    with SessionLocal() as db:
        db.execute(delete(RpAiPortal).where(RpAiPortal.eid.in_([EID, OTHER])))
        db.commit()


def _row(db, eid: str = EID) -> RpAiPortal | None:
    return db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))


# --------------------------------------------------------------------------
# 共享入口
# --------------------------------------------------------------------------

def test_a_new_row_gets_the_department_from_the_directory() -> None:
    """调用方没给部门时，从目录查一次。这是三条路径原本缺的那一步。"""
    with patch("app.services.portal_users._lookup_department", return_value=DEPT):
        with SessionLocal() as db:
            ensure_portal_user(db, EID)
            db.commit()
        with SessionLocal() as db:
            assert _row(db).department == DEPT


def test_a_supplied_department_skips_the_directory() -> None:
    """同步路径手上已经有部门，不该为每个作者再 bind 一次。"""
    with patch("app.services.portal_users._lookup_department") as looked_up:
        with SessionLocal() as db:
            ensure_portal_user(db, EID, department=DEPT)
            db.commit()
        looked_up.assert_not_called()
        with SessionLocal() as db:
            assert _row(db).department == DEPT


def test_an_empty_department_is_filled_in_on_a_later_pass() -> None:
    """只补空缺 —— 这正是历史欠的那些行自动补齐的机制。"""
    with SessionLocal() as db:
        db.add(RpAiPortal(eid=EID, display_name=EID, department=None))
        db.commit()
    with patch("app.services.portal_users._lookup_department", return_value=DEPT):
        with SessionLocal() as db:
            ensure_portal_user(db, EID)
            db.commit()
    with SessionLocal() as db:
        assert _row(db).department == DEPT


def test_a_department_that_is_already_set_is_never_overwritten() -> None:
    """非空的值可能是这个人自己登录时写的，比目录快照新。"""
    with SessionLocal() as db:
        db.add(RpAiPortal(eid=EID, display_name=EID, department="BNEW RTE RP PS FS"))
        db.commit()
    with patch("app.services.portal_users._lookup_department", return_value=DEPT):
        with SessionLocal() as db:
            ensure_portal_user(db, EID)
            db.commit()
    with SessionLocal() as db:
        assert _row(db).department == "BNEW RTE RP PS FS"


def test_a_blank_string_from_the_directory_is_not_a_department() -> None:
    """空白串必须当成"查不到"，否则库里会出现一个看起来有值的空部门。"""
    with patch("app.services.portal_users._lookup_department", return_value="   "):
        with SessionLocal() as db:
            ensure_portal_user(db, EID)
            db.commit()
    with SessionLocal() as db:
        assert _row(db).department is None


def test_a_directory_failure_does_not_block_the_row() -> None:
    """目录挂了不该拦下一次上传或一次收藏 —— 少一列远好过失败一次操作。"""
    with patch(
        "app.services.portal_users._lookup_department", side_effect=RuntimeError("ldap down")
    ):
        with SessionLocal() as db:
            with pytest.raises(RuntimeError):
                ensure_portal_user(db, EID)
    # 换成 helper 自己吞掉异常的那条路（_lookup_department 内部已 try/except）——
    # 这里直接验真实实现：把 DirectoryDepartments 弄坏，行仍然建得出来。
    with patch(
        "app.services.asset_owner.DirectoryDepartments", side_effect=RuntimeError("ldap down")
    ):
        with SessionLocal() as db:
            ensure_portal_user(db, OTHER)
            db.commit()
        with SessionLocal() as db:
            row = _row(db, OTHER)
            assert row is not None
            assert row.department is None


def test_the_helper_does_not_invent_a_display_name() -> None:
    """没给人名就留空 —— 不拿工号当人名。

    第一版这里写的是 `display_name or eid`，结果没有人名的行被填成工号，而下游把「有
    display_name」当成「知道这个人叫什么」，审核 MR 的标题就印成了 `TESTER (TESTER)`。
    是全量套件里 test_the_review_mr_names_the_uploader 把它抓出来的。
    """
    with patch("app.services.portal_users._lookup_department", return_value=DEPT):
        with SessionLocal() as db:
            ensure_portal_user(db, EID)
            db.commit()
    with SessionLocal() as db:
        assert _row(db).display_name is None


def test_origin_is_only_written_when_the_row_is_created() -> None:
    """已有行的 origin 一律不动：它可能是真的登录建的，一次导入不该把它降级。"""
    with SessionLocal() as db:
        db.add(RpAiPortal(eid=EID, display_name=EID, origin="login"))
        db.commit()
    with patch("app.services.portal_users._lookup_department", return_value=DEPT):
        with SessionLocal() as db:
            ensure_portal_user(db, EID, origin="import")
            db.commit()
    with SessionLocal() as db:
        assert _row(db).origin == "login"


def test_a_display_name_equal_to_the_eid_is_promoted_only_when_asked() -> None:
    """同步路径要能把占位的工号换成真名；交互路径不该动已有的名字。"""
    with SessionLocal() as db:
        db.add(RpAiPortal(eid=EID, display_name=EID))
        db.commit()
    with patch("app.services.portal_users._lookup_department", return_value=DEPT):
        with SessionLocal() as db:
            ensure_portal_user(db, EID, display_name="Real Name")
            db.commit()
        with SessionLocal() as db:
            assert _row(db).display_name == EID  # 默认不升级
        with SessionLocal() as db:
            ensure_portal_user(db, EID, display_name="Real Name", promote_display_name=True)
            db.commit()
        with SessionLocal() as db:
            assert _row(db).display_name == "Real Name"


# --------------------------------------------------------------------------
# 三条原本漏写的路径
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "module",
    ["app.routers.upload", "app.routers.market_favorites", "app.routers.agent_upload"],
)
def test_each_router_creates_the_row_through_the_shared_entry(module: str) -> None:
    """三处 ``_ensure_user`` 必须落到共享入口。

    按行为断言而不是按"函数体里有没有那行字"：这三处原来是三份各自的拷贝，只要还有一份自己
    建行，那条路径上的人就仍然没有部门，而三处长得一模一样、肉眼极难发现漏了哪一份。
    """
    import importlib

    mod = importlib.import_module(module)
    with patch("app.services.portal_users._lookup_department", return_value=DEPT):
        with SessionLocal() as db:
            mod._ensure_user(db, EID)
            db.commit()
    with SessionLocal() as db:
        row = _row(db)
        assert row is not None, f"{module} 没有建出行"
        assert row.department == DEPT, f"{module} 建行时没写 department"
