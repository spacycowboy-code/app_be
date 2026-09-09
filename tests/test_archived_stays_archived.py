"""下架过的技能不许被收敛器翻回来。

2026-09-09 线上的真实故障，这个文件是它的回归测试。

`rp_ai_skill.status` 这一列有两个写者：`skill_audit.reconcile` 从审计事实算出
approved / pending / rejected，而 `routers/skills.py` 的下架写第四个值 `archived`。
收敛器不认识第四个值，看见 `archived != approved` 就当成投影漂移纠正回去 —— 于是

* 02:20:58 下架返回 204，状态置成 archived；
* 02:24:06 收敛器把它写回 approved（它自己的日志：
  `reconciled 1 skill(s): {'digital-filter-for-allocation': 'approved'}`）；
* 技能重新出现在市场和部门统计里，而下架的人以为它已经没了。

这不是一次偶然：收敛器是独立容器按 `RECONCILE_INTERVAL`（默认 300 秒）常扫的，所以
**任何带审计记录的技能被下架后都会在一个扫描周期内复活**。当时生产 57 条技能里
`archived` 一条都没有，正是这条通病的痕迹。

所以这里钉三件事：下架免疫、免疫不过头（正常的投影仍然要算）、以及待复核的更新也不能
借着「MR 合了」把一个已下架的技能重新发布出来。
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.audit import RpAiSkillAuditRecord
from app.models.pending_update import RpAiSkillPendingUpdate
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.services.skill_audit import reconcile as reconcile_mod

OWNER = "EOWNER"
SID = "revive-probe"
MR = "http://gitlab/-/merge_requests/86"


def _seed(status: str, *, audit_state: str = "suspect", with_pending: bool = False) -> None:
    # 三步写：技能行的 `audit_ref` 与审计记录的 `skill_id` 互相引用，套件开了 sqlite 外键
    # 检查，所以一次 flush 里两行一起插必定撞 FK。
    with SessionLocal() as db:
        if db.get(RpAiPortal, OWNER) is None:
            db.add(RpAiPortal(eid=OWNER, display_name="Owner"))
            db.flush()
        db.add(
            RpAiSkill(
                id=SID, name="Revive Probe", category_id="public", status=status,
                version="1.0.0", tags=[], uploader_eid=OWNER,
                storage_path="public/revive-probe_EOWNER_Probe", repo_commit="a" * 40,
                instructions="# x\n", audit_state=audit_state,
            )
        )
        db.commit()

    with SessionLocal() as db:
        db.add(
            RpAiSkillAuditRecord(
                id="rec-" + SID,
                skill_id=SID,
                skill_name="Revive Probe",
                uploader_eid=OWNER,
                state=audit_state,
                mr_url=MR,
                replaces_published=False,
            )
        )
        db.commit()

    with SessionLocal() as db:
        db.get(RpAiSkill, SID).audit_ref = "rec-" + SID
        if with_pending:
            db.add(
                RpAiSkillPendingUpdate(
                    skill_id=SID, audit_ref="rec-" + SID, review_branch="review/x",
                    mr_url=MR, version="1.1.0",
                    storage_path="public/revive-probe_EOWNER_Probe",
                    commit_sha="b" * 40, tag="revive-probe@1.1.0",
                    artifact_name=None, editor_eid=OWNER, payload_json="{}",
                )
            )
        db.commit()


@pytest.fixture(autouse=True)
def _merged_review(monkeypatch):
    """复核 MR 已合并 —— 这正是把 status 算成 approved 的那个输入。

    线上那条 MR（rp-ai/rp-ai-skill !86）确实在 2026-09-07 被合并过，所以收敛器每一轮
    算出来的答案都是 approved。用 open 状态测不出这个缺陷。
    """
    monkeypatch.setattr(reconcile_mod.gitlab_mr, "mr_state", lambda url: "merged")


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with SessionLocal() as db:
        db.query(RpAiSkillPendingUpdate).filter_by(skill_id=SID).delete()
        row = db.get(RpAiSkill, SID)
        if row:
            db.delete(row)
        rec = db.get(RpAiSkillAuditRecord, "rec-" + SID)
        if rec:
            db.delete(rec)
        db.commit()


def test_an_archived_skill_is_left_archived(client):
    _seed("archived")
    with SessionLocal() as db:
        result = reconcile_mod.reconcile_all(db)
    assert result["changed"] == {}, "收敛器把一个已下架的技能改了"
    with SessionLocal() as db:
        assert db.get(RpAiSkill, SID).status == "archived"


def test_it_stays_archived_however_often_the_sweep_runs(client):
    """收敛器是常扫的，所以「扫一次没事」不够 —— 要的是不动点。"""
    _seed("archived")
    for _ in range(3):
        with SessionLocal() as db:
            assert reconcile_mod.reconcile_all(db)["changed"] == {}
    with SessionLocal() as db:
        assert db.get(RpAiSkill, SID).status == "archived"


def test_an_archived_skill_stays_out_of_the_public_list_after_a_sweep(client):
    """从用户那一侧看的同一件事：扫过之后它仍然不在市场上。

    这条是给「状态对了但别处又把它捞回来」留的口子 —— 列表是人真正会看的地方。
    """
    _seed("archived")
    with SessionLocal() as db:
        reconcile_mod.reconcile_all(db)
    assert SID not in [s["id"] for s in client.get("/api/skills").json()]


def test_a_staged_update_cannot_republish_an_archived_skill(client):
    """守卫必须在待复核那一段之前。

    那一段在 MR 合了的时候会把暂存的更新搬上来发布，落到一个已下架的技能上同样是复活它 ——
    而且是带着一个新版本号复活，比单纯改状态更难看出来。
    """
    _seed("archived", with_pending=True)
    with SessionLocal() as db:
        reconcile_mod.reconcile_all(db)
    with SessionLocal() as db:
        row = db.get(RpAiSkill, SID)
        assert row.status == "archived"
        assert row.version == "1.0.0", "暂存的更新被发布到一个已下架的技能上了"
        assert db.query(RpAiSkillPendingUpdate).filter_by(skill_id=SID).count() == 1, (
            "暂存行被消费掉了，说明那一段确实跑了"
        )


def test_the_guard_is_not_too_broad(client):
    """反面：正常的投影仍然要算出来。

    只钉「archived 不动」的话，一个 `return None` 放在函数最前面也能让上面几条全绿，而那
    等于把收敛器整个停掉。
    """
    _seed("pending")
    with SessionLocal() as db:
        assert reconcile_mod.reconcile_all(db)["changed"] == {SID: "approved"}
    with SessionLocal() as db:
        assert db.get(RpAiSkill, SID).status == "approved"
