"""待复核的更新:内容已经推到复核分支,但线上那一版一个字都没动。

**为什么需要这张表。** 一次更新触发人工复核时,已经在线上的那一版必须继续留在市场里 —— 它是被审过
的内容,而这次更新还不是。所以 ``rp_ai_skill`` 那一行(名称、版本、``storage_path``、
``repo_commit``、``status``)保持指向线上内容,这次更新的一切停在这里,等复核 MR 合了再一次性搬
过去(见 ``services/skill_audit/pending.publish``)。

原来没有这张表:更新直接写在技能行上,``status`` 置成 ``pending``。于是**一次触发复核的更新会让
已经发布的旧版本从市场消失** —— 列表按 ``status == "approved"`` 过滤;更糟的是 ``repo_commit``
被指到复核分支那个提交上,详情页于是按引用读到了那份没过审的正文。作者看到的是"我的 skill 不
见了",而它其实是被自己的一次更新顶掉的。

一个技能同时只有一个待复核的更新:再提一次就顶掉前一次(``skill_id`` 是主键)。两次更新是两份
内容,但"线上那一版之外还压着哪一份"只有一个答案,而那就是最新提交的那一份。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.skill import RpAiSkill

# 这次更新的下场。``pending`` = 还在复核里;``rejected`` = 复核 MR 被关掉了。
# 被拒的那一行**留着不删**:作者要能看到"我那次更新为什么一直没生效",而"复核里"和"被否了"是
# 两个不同的答案。再提一次更新会顶掉它(见 skill_audit.pending.stage)。
OUTCOME_PENDING = "pending"
OUTCOME_REJECTED = "rejected"
PENDING_OUTCOMES = (OUTCOME_PENDING, OUTCOME_REJECTED)


class RpAiSkillPendingUpdate(TimestampMixin, Base):
    __tablename__ = "rp_ai_skill_pending_update"

    # 一个技能一行:主键就是技能 id,再提一次更新覆盖这一行。
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="CASCADE"), primary_key=True
    )
    # 对应的审计记录 id(``rp_ai_skill_audit_record.id``)。故意不建外键:审计记录是只增不删的
    # 事实,而这一行是会被删的暂存;反过来的外键会让删这一行受制于那张表的删除策略。
    # 收敛器用它确认"最新那条可疑结论说的是这份暂存的更新,不是线上那一版"。
    audit_ref: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    review_branch: Mapped[str | None] = mapped_column(String(200))
    mr_url: Mapped[str | None] = mapped_column(String(512))
    # pending | rejected —— 见上面 PENDING_OUTCOMES。复核通过的那一份不会留在这里:它搬到技能行上
    # 之后这一行就删了,历史由版本记录和审计记录承担。
    outcome: Mapped[str] = mapped_column(
        String(16), default=OUTCOME_PENDING, nullable=False
    )

    # 复核通过后要搬到技能行上的仓库指针。
    version: Mapped[str | None] = mapped_column(String(32))
    storage_path: Mapped[str | None] = mapped_column(String(512))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    tag: Mapped[str | None] = mapped_column(String(128))
    artifact_name: Mapped[str | None] = mapped_column(String(256))

    # 谁提的这次更新。不建指向 ``rp_ai_portal`` 的外键:这一行的生命周期跟着复核走,不该因为
    # 用户表的删除策略而被动改写。
    editor_eid: Mapped[str | None] = mapped_column(String(64), index=True)

    # 门户侧那些字段的暂存值(名称、描述、标签、分组、示例,以及三个子集合)。正文的权威在仓库的
    # 复核分支上,所以这里只存仓库算不出来的那部分。键名见 ``skill_audit.pending.STAGED_SCALARS``。
    payload_json: Mapped[dict | None] = mapped_column(JSON)

    skill: Mapped["RpAiSkill"] = relationship(back_populates="pending_update")

    # 暂存里最常被问到的两格,读模型直接取用(``payload_json`` 是内部形状,不该出现在接口上)。
    @property
    def name(self) -> str | None:
        return (self.payload_json or {}).get("name")

    @property
    def description(self) -> str | None:
        return (self.payload_json or {}).get("description")
