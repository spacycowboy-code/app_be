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
