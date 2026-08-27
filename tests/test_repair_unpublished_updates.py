"""修复脚本本身要被测:它写的是生产库,而它认错一行的代价是把一个正常等复核的技能放上市场。"""
from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.models.audit import RpAiSkillAuditRecord
from app.models.category import RpAiCategory
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.models.version import RpAiSkillVersion
from tools import repair_unpublished_updates as repair_mod

MANIFEST = {
    "name": "Replir Allocation Tool",
    "description": "allocates replir",
    "category": "Public",
    "categoryId": "public",
    "tags": ["alloc"],
}


@pytest.fixture(autouse=True)
def _manifest_from_repo(monkeypatch):
    monkeypatch.setattr(
        repair_mod.skill_content, "read",
        lambda ref, path: json.dumps(MANIFEST).encode(),
    )


def _broken_skill(sid="replir", versions=2, status="pending") -> None:
    """造出 bug 留下的那种行:技能行被改成了没过审的那份,status 不是 approved。

    ``status`` 两种都要能造:复核人还没动那张 MR 时是 ``pending``,把 MR 关掉之后收敛器会把它
    推到 ``rejected``。两者是同一个坏行的两个时刻。
    """
    with SessionLocal() as db:
        if db.get(RpAiCategory, "public") is None:
            db.add(RpAiCategory(id="public", label="Public"))
        # 用户表的主键是自增 id,不是 EID —— 按 EID 查要用列条件(db.get 会拿它当主键,永远查不到,
        # 于是插进一行 eid=None 的用户,然后技能行的外键失败)。
        if db.query(RpAiPortal).filter(RpAiPortal.eid == "TESTER").first() is None:
            db.add(RpAiPortal(eid="TESTER"))
        db.flush()
        db.add(RpAiSkill(
            id=sid, name="Replir Allocation Tool (sudo)", description="now with sudo",
            category_id="public", category="Public", uploader_eid="TESTER",
            author="TESTER", version="1.1.0", repo_commit="suspectsha",
            storage_path="public/replir", status=status, audit_state="suspect",
            tags=["alloc", "sudo"],
        ))
        db.flush()
        rows = [("1.0.0", "livesha"), ("1.1.0", "suspectsha")][:versions]
        for version, sha in rows:
            db.add(RpAiSkillVersion(skill_id=sid, version=version, commit_sha=sha,
                                    tag=f"{sid}@{version}", uploader_eid="TESTER"))
        db.add(RpAiSkillAuditRecord(
            id="a" * 32, skill_id=sid, skill_name="Replir Allocation Tool",
            uploader_eid="TESTER", state="suspect",
            mr_url="http://gitlab/-/merge_requests/7",
        ))
        db.commit()


def test_a_dry_run_writes_nothing(capsys) -> None:
    _broken_skill()
    with SessionLocal() as db:
        assert repair_mod.repair(db, db.get(RpAiSkill, "replir"), apply=False) is False
        db.commit()
    with SessionLocal() as db:
        assert db.get(RpAiSkill, "replir").status == "pending"
    assert "1.1.0 -> 1.0.0" in capsys.readouterr().out


def test_apply_restores_the_published_version_from_the_repo() -> None:
    _broken_skill()
    with SessionLocal() as db:
        assert repair_mod.repair(db, db.get(RpAiSkill, "replir"), apply=True) is True
        db.commit()

    with SessionLocal() as db:
        skill = db.get(RpAiSkill, "replir")
        assert skill.status == "approved"                    # 又回到市场里
        assert (skill.version, skill.repo_commit) == ("1.0.0", "livesha")
        # 展示字段从那个提交的 manifest 复原 —— 不是在这里猜的。
        assert skill.name == "Replir Allocation Tool"
        assert skill.description == "allocates replir"
        assert skill.tags == ["alloc"]
        # 那次没过审的更新没丢,转成了待复核的暂存。
        pending = skill.pending_update
        assert pending.version == "1.1.0" and pending.commit_sha == "suspectsha"
        assert pending.mr_url == "http://gitlab/-/merge_requests/7"
        assert pending.payload_json["description"] == "now with sudo"
        # 没发布过的那条版本记录交给暂存行了。
        assert [v.version for v in skill.versions] == ["1.0.0"]


def test_a_skill_that_never_published_is_left_alone(capsys) -> None:
    """只有一条版本记录 = 从来没发布过。它在等复核是**对的**,放上市场才是 bug。"""
    _broken_skill(versions=1)
    with SessionLocal() as db:
        assert repair_mod.repair(db, db.get(RpAiSkill, "replir"), apply=True) is False
        db.commit()
    with SessionLocal() as db:
        assert db.get(RpAiSkill, "replir").status == "pending"
    assert "没有发布过的版本" in capsys.readouterr().out


def test_a_refused_update_is_repaired_too() -> None:
    """复核人把那张 MR 关掉之后,收敛器已经把这一行推到 ``rejected``。

    这是实际发生的那一种:人看到"这次更新不该上线"就关了 MR,不知道关掉的同时把**旧版本**也一起
    从市场上除名了。只认 ``pending`` 的话,修复脚本在真正需要它的场景下一行都匹配不到。
    """
    _broken_skill(status="rejected")
    with SessionLocal() as db:
        assert [s.id for s in repair_mod._candidates(db, None)] == ["replir"]
        assert repair_mod.repair(db, db.get(RpAiSkill, "replir"), apply=True) is True
        db.commit()
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, "replir")
        assert (skill.status, skill.version) == ("approved", "1.0.0")


def test_the_repair_survives_the_next_reconcile_sweep(monkeypatch) -> None:
    """修完之后收敛器不能把它又藏回去。

    那条可疑结论说的是没过审的那次更新,不是恢复回去的这一版。不把它标成 ``replaces_published``,
    下一轮收敛会把它当成当前这一版的结论:MR 已关 -> ``rejected``,技能几分钟后再次消失 ——
    修复自己撤销掉,而且看起来像是脚本没生效。
    """
    from app.services.skill_audit import reconcile as reconcile_mod

    _broken_skill(status="rejected")
    with SessionLocal() as db:
        repair_mod.repair(db, db.get(RpAiSkill, "replir"), apply=True)
        db.commit()

    monkeypatch.setattr(reconcile_mod.gitlab_mr, "mr_state", lambda url: "closed")
    for _ in range(3):
        with SessionLocal() as db:
            reconcile_mod.reconcile_all(db)
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, "replir")
        assert skill.status == "approved", "修复被收敛器撤销了"
        assert skill.version == "1.0.0"
        # 而作者仍然看得到那次更新被否了 —— 暂存行留着,状态是 rejected。
        assert skill.pending_update is not None
        assert skill.pending_update.outcome == "rejected"
