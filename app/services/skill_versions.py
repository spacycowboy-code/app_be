"""技能已发布版本号的查询。

单独一个模块而不是放在某个 router 里：上传（``routers/upload.py``）与编辑
（``routers/skills.py``）两条路都要用它，而 router 之间不该互相 import —— 依赖方向
一律是 router -> service。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.version import RpAiSkillVersion


def versions_taken(db: Session, skill_id: str) -> set[str]:
    """这个技能已经发布过的版本号，``skill_storage.plan_version`` 的撞号依据。

    **只当守卫用，不当自增基准。** 基准必须来自内容仓里那份 manifest（见
    ``skill_storage.store`` 里那段注释：从数据库加一会让两个并发发布算出同一个版本号，
    而 tag 冲突当时是默默跳过的，于是两份不同内容共用一个号、没人知道）。当守卫时这个
    读数旧一点没关系 —— 最多漏判一次撞号，随后写 tag 仍然会拦住。

    与 agent 侧 ``agent_upload._versions_taken`` 同一个形状，只是换了张表。
    """
    rows = db.scalars(
        select(RpAiSkillVersion.version).where(RpAiSkillVersion.skill_id == skill_id)
    ).all()
    return {(r or "").strip() for r in rows if r}
