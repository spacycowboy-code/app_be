"""把"一次更新触发安全复核就把旧版本顶掉"这个 bug 已经造成的行修回去。

修好的是**以后**:更新要复核时技能行不再被改动(见 services/skill_audit/pending.py)。但已经发生过
的那几次留下了坏行 —— 技能行被改成了那份没过审的内容(版本、``repo_commit``、名称、描述),
``status`` 是 ``pending``,于是市场上看不见它,详情页读到的还是复核分支上的东西。

这个脚本按仓库把它们复原:

* 认哪些行:``status == "pending"`` 且 ``audit_state == "suspect"`` 且**有两条以上版本记录**
  —— 最后一个条件是关键,它区分"曾经发布过、被一次更新顶掉了"和"从来没发布过、本来就在等复核"。
  后者是对的,不能动。
* 复原成什么:上一条版本记录(那才是发布过的那一版)的 version / commit / storage_path,以及
  那个提交上 ``manifest.json`` 里的名称、描述、标签、分组 —— 内容的权威在仓库,不在这里猜。
* 那次没过审的更新不丢:按新的形状写进 ``rp_ai_skill_pending_update``,复核 MR 合了照样能发布。
  对应的那条没发布的版本记录删掉(它现在由暂存行承担)。

默认只打印要做什么,不写库。真要写:``--apply``。

    python -m tools.repair_unpublished_updates                # 看一眼
    python -m tools.repair_unpublished_updates --apply        # 写
    python -m tools.repair_unpublished_updates --skill <id>   # 只修一个
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from app.database import SessionLocal
from app.models.pending_update import RpAiSkillPendingUpdate
from app.models.skill import RpAiSkill
from app.models.version import RpAiSkillVersion
from app.repositories import skill_content
from app.services.skill_audit.reconcile import latest_record

# manifest.json 的键 -> 技能行的列。只搬人写的那些,派生字段(评分、下载量)不在仓库里。
MANIFEST_FIELDS = {
    "name": "name",
    "description": "description",
    "category": "category",
    "categoryId": "category_id",
    "tags": "tags",
    "capabilityFull": "capability_full",
    "examplePrompt": "example_prompt",
    "exampleOutput": "example_output",
}


def _candidates(db, only: str | None) -> list[RpAiSkill]:
    stmt = select(RpAiSkill).where(
        RpAiSkill.status == "pending", RpAiSkill.audit_state == "suspect"
    )
    if only:
        stmt = stmt.where(RpAiSkill.id == only)
    return list(db.scalars(stmt).all())


def _versions(db, skill_id: str) -> list[RpAiSkillVersion]:
    return list(
        db.scalars(
            select(RpAiSkillVersion)
            .where(RpAiSkillVersion.skill_id == skill_id)
            .order_by(RpAiSkillVersion.id)
        ).all()
    )


def _manifest_at(commit: str | None, storage_path: str | None) -> dict:
    """那个提交上的 manifest.json。读不到就返回空 dict —— 那时只复原指针,不动展示字段。"""
    if not commit or not storage_path:
        return {}
    try:
        raw = skill_content.read(commit, f"{storage_path}/manifest.json")
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 读不到就降级,别让整批修复停下来
        print(f"    ! 读不到 {storage_path}/manifest.json@{commit[:8]}: {exc}")
        return {}


def repair(db, skill: RpAiSkill, *, apply: bool) -> bool:
    versions = _versions(db, skill.id)
    if len(versions) < 2:
        print(f"  跳过 {skill.id}: 只有 {len(versions)} 条版本记录 —— 它没有发布过的版本可以复原")
        return False

    unpublished, published = versions[-1], versions[-2]
    manifest = _manifest_at(published.commit_sha, skill.storage_path)
    record = latest_record(db, skill.id)

    print(f"  修 {skill.id}")
    print(f"    版本   {skill.version} -> {published.version}")
    print(f"    提交   {skill.repo_commit} -> {published.commit_sha}")
    print(f"    状态   pending -> approved")
    for key, column in MANIFEST_FIELDS.items():
        if key in manifest and manifest[key] != getattr(skill, column):
            print(f"    {column:16} {getattr(skill, column)!r} -> {manifest[key]!r}")
    print(f"    待复核的更新 v{unpublished.version} 转入暂存"
          f"(MR {record.mr_url if record else '未知'})")
    if not apply:
        return False

    staged = {
        column: getattr(skill, column) for column in MANIFEST_FIELDS.values()
    }
    db.merge(
        RpAiSkillPendingUpdate(
            skill_id=skill.id,
            audit_ref=record.id if record else "",
            review_branch=None,
            mr_url=record.mr_url if record else None,
            version=unpublished.version,
            storage_path=skill.storage_path,
            commit_sha=unpublished.commit_sha,
            tag=unpublished.tag,
            artifact_name=unpublished.artifact_name,
            editor_eid=unpublished.uploader_eid,
            payload_json=staged,
        )
    )
    for key, column in MANIFEST_FIELDS.items():
        if key in manifest:
            setattr(skill, column, manifest[key])
    skill.version = published.version
    skill.repo_commit = published.commit_sha
    skill.status = "approved"
    skill.audit_state = "pass"
    skill.audit_ref = None
    db.delete(unpublished)      # 这一版没发布过,它的历史现在由暂存行承担
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库(默认只打印)")
    ap.add_argument("--skill", help="只修这一个技能 id")
    args = ap.parse_args()

    with SessionLocal() as db:
        skills = _candidates(db, args.skill)
        if not skills:
            print("没有需要修的行。")
            return 0
        print(f"待检查 {len(skills)} 个 pending+suspect 的技能:")
        fixed = sum(repair(db, s, apply=args.apply) for s in skills)
        if args.apply:
            db.commit()
            print(f"已修 {fixed} 个。")
        else:
            print("以上是**预演**。加 --apply 才会写库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
