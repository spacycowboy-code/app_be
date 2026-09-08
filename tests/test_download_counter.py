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


def test_the_counter_is_written_from_the_event_table_not_from_its_own_value():
    """这一列**不是自增出来的**，是从事件表算出来后覆盖上去的。

    原本这里钉的是 SQL 里必须出现 ``downloads + 1``，防的是读改写的竞态（两个并发下载都
    读到 N，都写回 N+1，少算一次）。下载量的定义改成"有多少个不同的人"之后自增本身就错了：
    同一个人下十次会把这一列推到 10，而页面显示 1。

    覆盖式把那个竞态**一并消掉**了，而且这条断言更强：起始值故意设成一个荒谬的数，写完之后
    它必须等于事件表算出来的值 —— 也就是结果**与起始值无关**。读改写的实现过不了。
    """
    from app.routers.skills import _bump_downloads

    with SessionLocal() as db:
        skill = _skill(db, "s-absolute", 4242)  # 起始值是脏的
        db.commit()
        _bump_downloads(db, skill, eid="EONE")

    with SessionLocal() as db:
        assert db.get(RpAiSkill, "s-absolute").downloads == 1, (
            "写进去的值必须来自事件表；4242 泄漏进结果说明它还在拿旧值做加法"
        )


def test_two_people_count_twice_but_one_person_twice_counts_once():
    """数的是人，不是次数。

    原本这条断言两次下载加二,而那两次用的都是 ``eid=None`` —— 匿名。新规则下同一个条目的
    匿名行整体算**一个身份未知的下载者**(见 services/download_counts 里为什么不算 0、也不
    各算一个),所以两次匿名是 1。

    一并钉住两个人是 2,否则"永远返回 1"也能过上半条。
    """
    from app.routers.skills import _bump_downloads

    with SessionLocal() as db:
        skill = _skill(db, "s-twice", 0)
        db.commit()
        _bump_downloads(db, skill, eid=None)
        _bump_downloads(db, skill, eid=None)

    with SessionLocal() as db:
        assert db.get(RpAiSkill, "s-twice").downloads == 1, "两次匿名是一个未知下载者"

    with SessionLocal() as db:
        skill = _skill(db, "s-two-people", 0)
        db.commit()
        _bump_downloads(db, skill, eid="EONE")
        _bump_downloads(db, skill, eid="ETWO")

    with SessionLocal() as db:
        assert db.get(RpAiSkill, "s-two-people").downloads == 2


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
