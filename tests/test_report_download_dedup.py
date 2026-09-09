"""HTML 月报的下载口径也按 eid 去重。

这份报告和 ``/api/usage-report`` 那份 JSON 是**两套代码**（``services/usage_report/`` 对
``routers/usage.py``）。只改一边的后果是：同一个月的下载量，页面上一个数、发出去的月报另一个
数 —— 而月报是会被存档、拿去汇报的那一份，对不上的时候先被质疑的是页面。

这份报告原本刻意各记一次，理由写在 ``figures.py`` 里：「重复下载本身说明在反复用」。实测把
这个理由否掉了 —— 库里最高一条是同一工号对同一 skill 下载 93 次，相邻间隔中位数 1.0 秒、
92 个间隔里 88 个不到 5 秒。那是一个循环。

夹具照抄 ``test_report_counted_statuses`` 的形状：报告通过自己的连接读库，所以行必须**提交**
才看得见，也因此要显式清理而不是回滚。
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.download import RpAiSkillDownload
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.services.usage_report import figures
from app.services.usage_report.i18n import Strings

PREFIX = "dlded"
LOOP = "ELOOPXX"
CALM = "ECALMXX"


@pytest.fixture()
def seeded():
    with SessionLocal() as db:
        for eid in (LOOP, CALM):
            if db.get(RpAiPortal, eid) is None:
                db.add(RpAiPortal(eid=eid, display_name=eid))
        db.commit()
    with SessionLocal() as db:
        for n in (1, 2, 3):
            db.add(
                RpAiSkill(
                    id=f"{PREFIX}-s{n}",
                    name=f"Dedup Report Probe {n}",
                    status="approved",
                    tags=[],
                    uploader_eid=LOOP,
                )
            )
        db.add(
            RpAiMarketItem(
                id=f"{PREFIX}-item",
                name="Dedup Report Item",
                item_type="agent",
                service_type="cloud",
                status="approved",
                creator=LOOP,
            )
        )
        db.flush()
        # 同一个人对同一个 skill 下 40 次（就是那个循环的形状）
        for _ in range(40):
            db.add(RpAiSkillDownload(skill_id=f"{PREFIX}-s1", eid=LOOP))
        # 同一个人对**不同** skill 各一次 —— 这些必须各记一次
        db.add(RpAiSkillDownload(skill_id=f"{PREFIX}-s2", eid=LOOP))
        db.add(RpAiSkillDownload(skill_id=f"{PREFIX}-s3", eid=LOOP))
        # 另一个人拿了一次
        db.add(RpAiSkillDownload(skill_id=f"{PREFIX}-s1", eid=CALM))
        # agent 那条同理
        for _ in range(5):
            db.add(RpAiMarketDownload(item_id=f"{PREFIX}-item", eid=LOOP))
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(RpAiSkillDownload).filter(
            RpAiSkillDownload.skill_id.like(f"{PREFIX}%")
        ).delete(synchronize_session=False)
        db.query(RpAiMarketDownload).filter(
            RpAiMarketDownload.item_id.like(f"{PREFIX}%")
        ).delete(synchronize_session=False)
        db.query(RpAiSkill).filter(RpAiSkill.id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )
        db.query(RpAiMarketItem).filter(RpAiMarketItem.id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )
        db.commit()


def _metric(key: str):
    with SessionLocal() as db:
        return {m.key: m for m in figures.collect(db.connection())}[key]


def test_one_person_downloading_the_same_skill_many_times_counts_once(seeded) -> None:
    """40 次同一个 skill 记 1 —— 一个循环不该能顶起一张决策用的表。"""
    m = _metric("skill_download")
    assert m.per_eid.get(LOOP) == 3, "同一 skill 40 次记 1，加上另两个不同的 skill = 3"


def test_the_same_person_taking_different_skills_still_counts_each(seeded) -> None:
    """去重的是「同一个人拿同一个东西」，不是「同一个人」。

    这条防的是过度去重：把一个人拿了 3 个不同 skill 折成 1，会让最活跃的使用者看起来和
    只拿过一次的人一样 —— 那是往反方向错，而且同样不报错。
    """
    m = _metric("skill_download")
    assert m.per_eid.get(LOOP) == 3
    assert m.per_eid.get(CALM) == 1


def test_agent_use_follows_the_same_rule(seeded) -> None:
    """agent 与 skill 必须一条口径，否则月报内部两节自己对不上。"""
    m = _metric("agent_use")
    assert m.per_eid.get(LOOP) == 1, "同一个 agent 取用 5 次记 1"


def test_the_printed_unit_no_longer_says_times(seeded) -> None:
    """单位和说明是**报告里印出来的字**。

    数改了而单位还写「次下载」，报告会一边显示去重后的数、一边在脚注里声明重复各记一次。
    自相矛盾的报告比数字偏了更糟：读的人不知道该信哪一边。
    """
    for key in ("skill_download", "agent_use"):
        m = _metric(key)
        assert "次" not in m.unit, f"{key} 的单位仍是次数口径：{m.unit!r}"
        # 断言"只记一次"这句在，而不是"各记一次"不在 —— 新说明里也有"各记一次",讲的是
        # 同一人拿**不同**东西各记一次。用后者做断言会咬到正确的文字上。
        assert "只记一次" in m.note, f"{key} 的说明没写清去重口径"


# --------------------------------------------------------------------------
# 贡献者表的「被取用」列
# --------------------------------------------------------------------------

def _contrib(eid: str):
    with SessionLocal() as db:
        rows, _gaps = figures.contributions(db.connection())
    return rows.get(eid.upper())


def test_the_taken_column_counts_take_ups_not_downloads(seeded) -> None:
    """一个人反复下载同一个 skill 不该抬高作者的贡献数。

    线上 Kasan Li 是 ``12 / 109``，而那 109 里有 **93 次是他自己下载他自己上传的
    outlook-assistant**，相邻间隔中位数 1.0 秒。按次数算，一个反复下载的脚本就能把任何人
    顶到贡献名单最前面，而这张表是拿来认人、拿来做外联的。
    """
    r = _contrib(LOOP)
    assert r is not None, "作者没进贡献者表"
    # s1 被 LOOP 下了 40 次 + CALM 1 次，s2/s3 各被 LOOP 下 1 次
    # 去重后的取用数 = (s1,LOOP) (s1,CALM) (s2,LOOP) (s3,LOOP) = 4
    assert r["skill_taken"] == 4, "被取用数必须是不同的 (技能, 人) 组合"
    assert r["skill_takers"] == 2, "取用过的人数不该被这次改动影响"


def test_the_two_columns_are_still_different_numbers(seeded) -> None:
    """去重**没有**让两列变成同一个数 —— 我上一版判断错了，这条钉住它。

    ``takers`` 是取用过此人东西的人数，``taken`` 是不同的 (技能, 人) 组合。一个人拿走同一
    作者的 3 个 skill 就是 ``1 / 3``。只有单技能作者两列才相等（线上 Kasan Li 恰好是），
    那是数据使然、不是口径塌了。
    """
    r = _contrib(LOOP)
    assert r["skill_takers"] == 2
    assert r["skill_taken"] == 4
    assert r["skill_takers"] != r["skill_taken"], (
        "LOOP 一个人拿了 3 个 skill、CALM 拿了 1 个，两列必须不同"
    )


def test_the_agent_side_follows_the_same_rule(seeded) -> None:
    """两个市场在同一张表里用两套口径，读表的人无从察觉。"""
    with SessionLocal() as db:
        db.execute(
            RpAiMarketItem.__table__.update()
            .where(RpAiMarketItem.id == f"{PREFIX}-item")
            .values(creator=LOOP)
        )
        db.commit()
    r = _contrib(LOOP)
    assert r["market_taken"] == 1, "同一个 agent 取用 5 次记 1"
    assert r["market_takers"] == 1


def test_the_column_label_no_longer_says_times(seeded) -> None:
    """表头单位与导语是**印在报告上的字**。

    数改了而文案还写「次数」，报告会一边显示去重后的数、一边告诉读者这是下载次数 ——
    自相矛盾的报告比数字偏了更糟，读的人不知道该信哪一边。中英两套都要改。
    """
    for lang, forbidden, required in (
        ("zh", "人数 / 次数", "取用数"),
        ("en", "people / times", "take-ups"),
    ):
        st = Strings(lang)
        unit = st["col_contrib_taken_unit"]
        assert unit != forbidden, f"{lang} 表头单位还是旧口径：{unit!r}"
        assert required in unit, f"{lang} 表头单位没写清新口径：{unit!r}"
        lead = st["lead_people_2"]
        assert required in lead, f"{lang} 导语没解释新口径"
        assert "93" in lead, f"{lang} 导语没给出为什么改的证据"


def test_the_non_directory_note_does_not_call_them_downloads(seeded) -> None:
    """那条脚注把同一个数说成「被取用 N 次」，也得跟着改。

    单独一条是因为它在另一个键里，改文案时最容易漏掉 —— 而它印在同一页上。
    """
    for lang, marker in (("zh", "去重"), ("en", "deduplicated")):
        assert marker in Strings(lang)["note_non_dir"], f"{lang} 脚注没说明口径"
