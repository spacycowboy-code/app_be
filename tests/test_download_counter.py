"""下载计数是整数,不是字符串。

这一列建表时是 ``VARCHAR(32)``,注释里的理由是"前端显示成 5.6K 这样的格式化字符串" —— 前端从
未那么做:模板直接渲染原始值,排序还要先 ``parseInt``。也就是说页面上看到的顺序一直是对的,是**调用
方在替存储兜底**,所以这个缺陷不表现为界面出错,只在有人写 SQL 或者两次下载撞在一起时才现形。

因此这里钉三件事,每一件对应一个真实代价:
1. SQL 层的 ``ORDER BY`` 按数值排 —— 字符串列会把 "43" 排在 "5" 后面。
2. 自增在数据库里做,并发下不丢计数 —— 读-改-写会让两个并发下载都写 N+1。
3. 外部来源的怪值(``"5.6K"``)在写入前就被转成 0,不会让整批导入炸掉。
"""
from __future__ import annotations

import pytest
from sqlalchemy import Integer, select

from app.database import SessionLocal
from app.models.skill import RpAiSkill, as_download_count


def _skill(db, sid: str, downloads: int) -> RpAiSkill:
    s = RpAiSkill(
        id=sid,
        name=sid,
        status="approved",
        category_id="public",
        downloads=downloads,
    )
    db.add(s)
    return s


def test_the_column_is_an_integer():
    """类型本身就是结论,别只测行为 —— 行为是能被调用方的 parseInt 掩盖的。"""
    assert isinstance(RpAiSkill.__table__.c.downloads.type, Integer)
    assert RpAiSkill.__table__.c.downloads.nullable is False


def test_sql_orders_downloads_by_value_not_by_text():
    """``ORDER BY downloads`` 要按数值排。

    这是字符串列唯一会直接看出来的症状:字典序下 "43" < "5",于是下载 43 次的技能排在下载 5 次的
    后面。之所以线上页面没出错,是因为前端排序先 ``parseInt`` —— 存储的问题被调用方挡住了,而下
    一个直接写 SQL 的人挡不住。
    """
    with SessionLocal() as db:
        _skill(db, "s-five", 5)
        _skill(db, "s-fortythree", 43)
        _skill(db, "s-nine", 9)
        db.commit()

        order = list(
            db.scalars(select(RpAiSkill.id).order_by(RpAiSkill.downloads.desc()))
        )

    assert order == ["s-fortythree", "s-nine", "s-five"], (
        "按数值排应该是 43 > 9 > 5;如果得到 9 > 5 > 43,说明这一列又变回字符串了"
    )


def test_the_increment_happens_in_sql_not_in_python():
    """自增必须是 ``downloads = downloads + 1``,由数据库做加法。

    这条钉的是**原子性**,而原子性没法靠"起两个会话各加一次"来测 —— 那样两边是串行的,后一个会读到
    前一个提交后的值,加出来的结果恰好是对的,于是旧的读-改-写实现也照样通过。我试过,它确实通过了,
    所以那种测试是假的,已经删掉。

    能真正区分两种实现的是**发给数据库的语句**:在库里做加法,SET 右边引用列本身;在 Python 里算好
    再写,SET 右边是一个绑定参数。所以这里抓真实 SQL 来判断。

    为什么在意:旧代码读出 N、算 N+1、写回去。两个并发下载都读到 N,都写 N+1,少记一次。这个错不会
    报警,只会让下载数长期偏低 —— 正是那种"看不见的错误"。
    """
    from sqlalchemy import event

    from app.database import engine
    from app.routers.skills import _bump_downloads

    seen: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "UPDATE rp_ai_skill" in statement and "downloads" in statement:
            seen.append(" ".join(statement.split()))

    with SessionLocal() as db:
        skill = _skill(db, "s-sql", 10)
        db.commit()

        event.listen(engine, "before_cursor_execute", capture)
        try:
            _bump_downloads(db, skill, eid=None)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    assert seen, "没有抓到自增用的 UPDATE 语句"
    stmt = seen[0]
    assert "downloads + " in stmt, (
        f"SET 右边必须引用列本身,否则加法就回到了 Python 里。实际语句:{stmt}"
    )

    with SessionLocal() as db:
        assert db.get(RpAiSkill, "s-sql").downloads == 11


def test_two_downloads_count_twice():
    """两次下载加二 —— 比上一条弱,只保证没数错,不保证并发安全。两条各管一件事。"""
    from app.routers.skills import _bump_downloads

    with SessionLocal() as db:
        skill = _skill(db, "s-twice", 0)
        db.commit()
        _bump_downloads(db, skill, eid=None)
        _bump_downloads(db, skill, eid=None)

    with SessionLocal() as db:
        assert db.get(RpAiSkill, "s-twice").downloads == 2


@pytest.mark.parametrize(
    "raw, want",
    [
        (7, 7),
        ("7", 7),
        (" 7 ", 7),
        (7.9, 7),
        (None, 0),
        ("", 0),
        ("5.6K", 0),      # 上游 manifest 真出现过的写法
        ("很多", 0),
        (-3, 0),          # 负的下载数没有意义
        ("-3", 0),
        (True, 0),        # bool 是 int 的子类,但它不是一个计数
    ],
)
def test_external_values_are_coerced_before_they_reach_the_column(raw, want):
    """外部 JSON 的值在写库之前就转好,转不了归零而不是抛异常。

    种子数据和上游官方目录的 manifest 不是我们写的。以前列是字符串,``"5.6K"`` 能原样存进去,
    然后每个读取方各自防一手。现在列是整数,转换必须前移 —— 而且不能因为一个展示用的计数格式不对
    就让整批导入失败。
    """
    assert as_download_count(raw) == want


def test_a_coerced_value_actually_stores():
    """转换的结果要真能落库 —— 只测纯函数会漏掉类型不匹配。"""
    with SessionLocal() as db:
        _skill(db, "s-coerced", as_download_count("5.6K"))
        db.commit()
        assert db.get(RpAiSkill, "s-coerced").downloads == 0
