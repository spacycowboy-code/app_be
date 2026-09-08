"""下载量按 eid 去重 —— 一条规则，所有出口都得一样。

改之前同一个概念在仓里有两套算法：agent 市场卡片用 ``count(distinct eid)``，技能卡片读
``rp_ai_skill.downloads`` 累计列（每次 +1），盘点和 admin 月报用 ``func.count()``。于是同一个
人下载同一个技能 10 次，在 agent 市场算 1、在技能市场算 10 —— **两个页面对同一件事给出不同的
数，而这不报错**。

线上真实数据：``outlook-assistant`` 的 93 次下载全部来自一个工号，相邻间隔中位数 1.0 秒。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.database import SessionLocal
from app.models.download import RpAiSkillDownload
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.services.asset_stats import attach_market_stats, attach_skill_stats
from app.services.download_counts import (
    MARKET,
    SKILL,
    unique_downloads,
    unique_downloads_by_department,
    unique_downloads_total,
)

SK = "dedup-probe-skill"
IT = "dedup-probe-item"
NOW = datetime(2026, 9, 1, 12, 0, 0)


@pytest.fixture()
def seeded():
    """一个技能 + 一个市场条目，各带一组刻意重复的下载记录。"""
    with SessionLocal() as db:
        db.execute(delete(RpAiSkillDownload).where(RpAiSkillDownload.skill_id == SK))
        db.execute(delete(RpAiMarketDownload).where(RpAiMarketDownload.item_id == IT))
        db.execute(delete(RpAiSkill).where(RpAiSkill.id == SK))
        db.execute(delete(RpAiMarketItem).where(RpAiMarketItem.id == IT))
        # tags 必须是列表：SkillSummary 的 tags 不接受 None，而列表端点会把整份响应判为无效。
        # uploader_eid 是指向 rp_ai_portal 的外键，所以先把这个人建出来。
        if db.scalar(select(RpAiPortal).where(RpAiPortal.eid == "EOWNER")) is None:
            db.add(RpAiPortal(eid="EOWNER", display_name="Probe Owner"))
            db.flush()
        db.add(RpAiSkill(id=SK, name="Dedup Probe", downloads=999,
                         status="approved", tags=[], uploader_eid="EOWNER"))
        db.add(RpAiMarketItem(id=IT, name="Dedup Probe Item", item_type="agent",
                              service_type="code", status="approved",
                              creator="EOWNER"))
        db.flush()
        # 同一个人下 5 次 + 另一个人 1 次 = 2 个下载者，6 行。
        for i in range(5):
            db.add(RpAiSkillDownload(skill_id=SK, eid="EBUSY", department="D-ONE",
                                     created_at=NOW + timedelta(seconds=i)))
        db.add(RpAiSkillDownload(skill_id=SK, eid="ECALM", department="D-TWO",
                                 created_at=NOW + timedelta(minutes=1)))
        for i in range(3):
            db.add(RpAiMarketDownload(item_id=IT, eid="EBUSY", department="D-ONE",
                                      created_at=NOW + timedelta(seconds=i)))
        db.commit()
    yield
    with SessionLocal() as db:
        db.execute(delete(RpAiSkillDownload).where(RpAiSkillDownload.skill_id == SK))
        db.execute(delete(RpAiMarketDownload).where(RpAiMarketDownload.item_id == IT))
        db.execute(delete(RpAiSkill).where(RpAiSkill.id == SK))
        db.execute(delete(RpAiMarketItem).where(RpAiMarketItem.id == IT))
        db.commit()


# --------------------------------------------------------------------------
# 规则本身
# --------------------------------------------------------------------------

def test_one_person_downloading_five_times_counts_once(seeded) -> None:
    with SessionLocal() as db:
        assert unique_downloads(db, SKILL, [SK])[SK] == 2  # 6 行 -> 2 个人
        assert unique_downloads(db, MARKET, [IT])[IT] == 1  # 3 行 -> 1 个人


def test_anonymous_rows_count_as_one_unknown_downloader(seeded) -> None:
    """``count(distinct eid)`` 会整体忽略 NULL —— 只用它,匿名下载会算成 0 次。

    线上技能表有 10 行 eid 为空(2026-08-06..12,那时下载端点还不要求身份)。一个真被下载过
    的东西显示 0 是明摆着的假话;而把 10 行各算一个人也不对,其中 6 行在同一天 12 分钟内。
    """
    with SessionLocal() as db:
        for i in range(4):
            db.add(RpAiSkillDownload(skill_id=SK, eid=None,
                                     created_at=NOW + timedelta(hours=i)))
        db.commit()
        # EBUSY + ECALM + 一个未知的 = 3，不是 2（忽略匿名）也不是 6（各算一个）
        assert unique_downloads(db, SKILL, [SK])[SK] == 3


def test_an_item_downloaded_only_anonymously_is_not_zero(seeded) -> None:
    """只有匿名下载的条目必须 >= 1 —— 这是上一条测试真正防住的失效方式。"""
    with SessionLocal() as db:
        db.execute(delete(RpAiSkillDownload).where(RpAiSkillDownload.skill_id == SK))
        db.add(RpAiSkillDownload(skill_id=SK, eid=None, created_at=NOW))
        db.add(RpAiSkillDownload(skill_id=SK, eid=None, created_at=NOW))
        db.commit()
        assert unique_downloads(db, SKILL, [SK])[SK] == 1


def test_dedup_happens_inside_the_reporting_window(seeded) -> None:
    """同一个人这个月拿一次、下个月再拿一次,两个月各算一次。

    月报问的是"这个月有多少人拿了它",不是"史上有多少人拿过"。全局去重会让第二个月显示 0,
    看上去像没人用了。
    """
    with SessionLocal() as db:
        later = NOW + timedelta(days=40)
        db.add(RpAiSkillDownload(skill_id=SK, eid="EBUSY", created_at=later))
        db.commit()
        m1 = unique_downloads(db, SKILL, [SK], since=NOW - timedelta(days=1),
                              until=NOW + timedelta(days=1))
        m2 = unique_downloads(db, SKILL, [SK], since=later - timedelta(days=1),
                              until=later + timedelta(days=1))
        assert m1[SK] == 2
        assert m2[SK] == 1, "第二个窗口里 EBUSY 又拿了一次，必须算一次"


def test_the_total_agrees_with_the_per_item_numbers(seeded) -> None:
    """总数和明细必须同一个口径 —— 对不上的时候没人能一眼看出是哪边错。"""
    with SessionLocal() as db:
        per_item = unique_downloads(db, SKILL)
        assert unique_downloads_total(db, SKILL) == sum(per_item.values())


# --------------------------------------------------------------------------
# 按部门（月报 Group activity）
# --------------------------------------------------------------------------

def test_a_person_who_changed_department_is_not_counted_twice(seeded) -> None:
    """先去重再归部门。反过来会让换过组的人在两个部门各算一次,两个组都变大、合计也变大。"""
    with SessionLocal() as db:
        # EBUSY 换组之后又下载了同一个技能
        db.add(RpAiSkillDownload(skill_id=SK, eid="EBUSY", department="D-THREE",
                                 created_at=NOW + timedelta(days=1)))
        db.commit()
        by_dept = unique_downloads_by_department(db, SKILL)
        mine = {k: v for k, v in by_dept.items() if k in {"D-ONE", "D-TWO", "D-THREE"}}
        assert mine.get("D-THREE") is None, "换组后的那次不该另算一个人"
        assert mine.get("D-ONE") == 1
        assert sum(mine.values()) == unique_downloads(db, SKILL, [SK])[SK]


def test_the_representative_row_is_stable_across_runs(seeded) -> None:
    """同一份报告重跑两次必须给出同样的分组 —— 不可复现的报告比有偏差的更难用。"""
    with SessionLocal() as db:
        first = unique_downloads_by_department(db, SKILL)
        second = unique_downloads_by_department(db, SKILL)
        assert first == second


# --------------------------------------------------------------------------
# 出口：两个市场的卡片必须给出同一个数
# --------------------------------------------------------------------------

def test_both_markets_answer_with_unique_downloaders(seeded) -> None:
    """技能卡片和 agent 卡片对"同一个人下载多次"必须给出同样的答案。"""
    with SessionLocal() as db:
        skill = db.scalar(select(RpAiSkill).where(RpAiSkill.id == SK))
        attach_skill_stats(db, [skill])
        item = db.scalar(select(RpAiMarketItem).where(RpAiMarketItem.id == IT))
        attach_market_stats(db, [item])
        assert skill.downloads == 2
        assert item.download_count == 1


def test_the_stale_counter_column_does_not_leak_into_the_card(seeded) -> None:
    """夹具把累计列设成 999(线上那一列真的和事件表漂了 399 vs 356)。

    卡片必须报事件表算出来的数,而不是这个列。这条钉的是"真相只有一处"。
    """
    with SessionLocal() as db:
        skill = db.scalar(select(RpAiSkill).where(RpAiSkill.id == SK))
        assert skill.downloads == 999  # 读出来确实是脏的
        attach_skill_stats(db, [skill])
        assert skill.downloads == 2


def test_the_detail_endpoint_agrees_with_the_list(seeded, client) -> None:
    """详情端点自己算统计、不走 attach_skill_stats —— 漏了它两个页面就显示两个数。"""
    r = client.get(f"/api/skills/{SK}")
    assert r.status_code == 200, r.text
    assert r.json()["downloads"] == 2

    r = client.get("/api/skills")
    assert r.status_code == 200, r.text
    listed = [s for s in r.json() if s["id"] == SK]
    assert listed and listed[0]["downloads"] == 2


def test_the_uploaded_skills_endpoint_agrees_too(seeded, client) -> None:
    """`/api/users/{eid}/skills` 发的也是 SkillSummary，此前直接返回 ORM 行。"""
    r = client.get("/api/users/EOWNER/skills")
    assert r.status_code == 200, r.text
    mine = [s for s in r.json() if s["id"] == SK]
    assert mine and mine[0]["downloads"] == 2
