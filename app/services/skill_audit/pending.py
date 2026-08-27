"""待复核更新的暂存与落地。

一条规则:**要人工复核的更新不改动已发布的那一版。** 内容推到复核分支,门户侧那些字段暂存在
``rp_ai_skill_pending_update``,技能行继续描述线上内容。复核 MR 合了,这里把暂存的一切搬到技能
行上(这时才写版本历史、才通知收藏者);MR 被关掉,就把暂存丢掉,线上那一版本来就没动过。

三个调用方,一份逻辑:上传路径、编辑路径(暂存),以及收敛器(落地/丢弃)。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.audit import RpAiSkillAuditRecord
from app.models.pending_update import (
    OUTCOME_PENDING,
    OUTCOME_REJECTED,
    RpAiSkillPendingUpdate,
)
from app.models.skill import RpAiInputParam, RpAiSkill, RpAiSkillCase, RpAiSubSkill
from app.models.version import RpAiSkillVersion
from app.services.notify import notify_version_update

log = logging.getLogger("skill_audit.pending")

# 门户侧、仓库里算不出来的那些字段。正文与附件的权威在复核分支上,所以不进这个快照。
STAGED_SCALARS = (
    "name",
    "description",
    "category",
    "category_id",
    "image",
    "is_free",
    "tags",
    "capability_full",
    "example_prompt",
    "example_output",
)


def capture(skill: RpAiSkill) -> dict:
    """把技能行上**这次更新后**的值抄一份下来。

    在回滚之前调用:两条写入路径都是先把改动落在 ORM 对象上、再拿它去审计(审的就是这份改动渲染
    出来的内容),所以此刻对象上带的正是要暂存的值。抄完再回滚,数据库里那一行就回到线上那一版。
    """
    data = {key: getattr(skill, key) for key in STAGED_SCALARS}
    data["cases"] = [{"title": c.title, "image": c.image} for c in skill.cases]
    data["sub_skills"] = [
        {"name": s.name, "description": s.description, "badge": s.badge,
         "icon_color": s.icon_color, "icon_bg": s.icon_bg}
        for s in skill.sub_skills
    ]
    data["input_params"] = [
        {"name": p.name, "type": p.type, "required": p.required,
         "description": p.description}
        for p in skill.input_params
    ]
    return data


def for_skill(db: Session, skill_id: str) -> RpAiSkillPendingUpdate | None:
    return db.get(RpAiSkillPendingUpdate, skill_id)


def awaiting_review(db: Session, skill_id: str) -> RpAiSkillPendingUpdate | None:
    """还在等复核的那一份(被拒的那一行留着给作者看,但它不再等任何人)。"""
    row = db.get(RpAiSkillPendingUpdate, skill_id)
    return row if row is not None and row.outcome == OUTCOME_PENDING else None


def stage(
    db: Session,
    *,
    skill_id: str,
    staged: dict,
    audit_ref: str,
    review_branch: str | None,
    mr_url: str | None,
    version: str | None,
    storage_path: str | None,
    commit_sha: str | None,
    tag: str | None,
    artifact_name: str | None,
    editor_eid: str | None,
) -> RpAiSkillPendingUpdate:
    """记下"线上那一版之外还压着这一份"。已有一份就顶掉:最新提交的那次更新才是待复核的那次。"""
    row = db.get(RpAiSkillPendingUpdate, skill_id)
    if row is None:
        row = RpAiSkillPendingUpdate(skill_id=skill_id)
        db.add(row)
    row.audit_ref = audit_ref
    row.review_branch = review_branch
    row.mr_url = mr_url
    row.version = version
    row.storage_path = storage_path
    row.commit_sha = commit_sha
    row.tag = tag
    row.artifact_name = artifact_name
    row.editor_eid = editor_eid
    row.payload_json = staged
    # 重新提交会顶掉上一次的下场:等复核的是这一份。
    row.outcome = OUTCOME_PENDING
    return row


def publish(db: Session, skill: RpAiSkill, pending: RpAiSkillPendingUpdate) -> None:
    """复核通过:把暂存的更新搬到技能行上,它现在就是线上那一版。

    版本历史与"收藏的技能更新了"这条通知都在这里才发生,不在提交更新的时候 —— 一次还没发布的更新
    不是一次更新:announce 出去,首页的 What's New 会推一个别人根本装不到的版本。
    """
    data = pending.payload_json or {}
    for key in STAGED_SCALARS:
        if key in data:
            setattr(skill, key, data[key])
    if "cases" in data:
        skill.cases = [RpAiSkillCase(**c) for c in data["cases"]]
    if "sub_skills" in data:
        skill.sub_skills = [RpAiSubSkill(**s) for s in data["sub_skills"]]
    if "input_params" in data:
        skill.input_params = [RpAiInputParam(**p) for p in data["input_params"]]

    skill.version = pending.version or skill.version
    skill.storage_path = pending.storage_path or skill.storage_path
    skill.repo_commit = pending.commit_sha or skill.repo_commit
    skill.status = "approved"
    # 线上这一版的结论确实是"可疑",只是有人读过之后放行了。这一格是给人看的,不是闸门。
    skill.audit_state = "suspect"
    # 从现在起,这一版对应的结论就是那条记录(收敛器据此重算,不靠时间戳排序)。
    skill.audit_ref = pending.audit_ref

    if pending.version:
        db.add(
            RpAiSkillVersion(
                skill_id=skill.id,
                version=pending.version,
                commit_sha=pending.commit_sha,
                tag=pending.tag,
                uploader_eid=pending.editor_eid or skill.uploader_eid,
                artifact_name=pending.artifact_name,
                notes="复核通过后发布",
            )
        )
        notify_version_update(db, skill, pending.version,
                              actor_eid=pending.editor_eid)
    # 这条审计结论从现在起说的就是发布出去的那一版了 —— 它不再是"线上之外的另一份内容"。翻掉这一格,
    # 收敛器下一轮才会把它当成当前版本的结论(可疑 + MR 已合 = 已发布),而不是跳过它。
    record = db.get(RpAiSkillAuditRecord, pending.audit_ref)
    if record is not None:
        record.replaces_published = False
    db.delete(pending)
    log.info("published pending update for %s (v%s)", skill.id, pending.version)


def reject(db: Session, pending: RpAiSkillPendingUpdate) -> None:
    """复核 MR 被关掉:这次更新不发布。

    行留着不删,原因是作者需要一个答案 —— "复核里"和"被否了"不能长得一样,而这一行是唯一能回答
    后者的地方。线上那一版从头到尾没被动过,所以这里不用恢复任何东西。
    """
    pending.outcome = OUTCOME_REJECTED
    log.info("pending update for %s rejected (MR closed)", pending.skill_id)


def drop(db: Session, pending: RpAiSkillPendingUpdate, *, why: str) -> None:
    """把暂存的更新丢掉 —— 用于它被一次直接发布的写入取代,不是用于复核结论。"""
    log.info("dropping pending update for %s: %s", pending.skill_id, why)
    db.delete(pending)
