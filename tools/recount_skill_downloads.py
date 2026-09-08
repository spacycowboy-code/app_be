"""把 ``rp_ai_skill.downloads`` 这一列重算成**去重后的下载人数**。默认 dry run。

## 为什么需要它

下载量的定义改成了"有多少个不同的人下载过"（见 ``services/download_counts``）。所有页面与
报告都已经改成从事件表现算，所以**页面上的数字不依赖这个脚本**。但这一列还被留着当缓存，
而它存的是旧定义下的累计次数，两者对不上。跑一次让库里的值和页面一致。

``_bump_downloads`` 从此写去重后的值，是自愈的 —— 任何技能下一次被下载时这一列会自动归位。
这个脚本只是不想等"下一次被下载"，因为有的技能可能几个月不会再被下载一次。

## 有一部分数字会变小，而且补不回来

线上这一列合计 **399**，事件表只有 **356** 行。差的 43 次是事件表建起来（最早一条
2026-08-06）之前的下载：那些行**从来没记录是谁下的**，"按 eid 去重"对它们根本无定义。

所以重算之后：

* `KSDB Insights` 累计列 2、事件表 0 行 -> 变成 **0**。它确实被下载过两次,但那两次没有留下
  任何身份,新定义下无法计入。
* `outlook-assistant` 的 93 次里有 92 次来自同一个工号 `EYLAIXN`(相邻间隔中位数 1.0 秒,
  是个循环)-> 变成 1。这不是数据丢失,这正是要修的东西。

**这个脚本不编造数字**:它只把这一列设成事件表算出来的值。旧的累计值在跑之前会打印出来,
需要留档的话先把输出存下来。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402
from app.models.skill import RpAiSkill  # noqa: E402
from app.services.download_counts import SKILL, unique_downloads  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true", help="真的写库；不加就只打印将要发生什么"
    )
    args = ap.parse_args()

    with SessionLocal() as db:
        fresh = unique_downloads(db, SKILL)
        rows = db.execute(select(RpAiSkill.id, RpAiSkill.name, RpAiSkill.downloads)).all()

        changes = []
        for sid, name, old in rows:
            new = int(fresh.get(sid, 0))
            if int(old or 0) != new:
                changes.append((sid, name, int(old or 0), new))

        print(f"=== 技能共 {len(rows)} 个，其中 {len(changes)} 个的累计列与去重值不同 ===")
        print(f"{'名字':<34} {'现在':>5} {'去重后':>6} {'差':>6}")
        for _sid, name, old, new in sorted(changes, key=lambda c: c[2] - c[3], reverse=True):
            print(f"{str(name)[:34]:<34} {old:>5} {new:>6} {new - old:>+6}")

        old_total = sum(int(r[2] or 0) for r in rows)
        new_total = sum(int(fresh.get(r[0], 0)) for r in rows)
        print()
        print(f"    合计 {old_total} -> {new_total}（{new_total - old_total:+d}）")

        if not args.apply:
            print("\n这是 dry run，一行都没动。加 --apply 才会写。")
            return 0

        for sid, _name, _old, new in changes:
            db.execute(
                RpAiSkill.__table__.update()
                .where(RpAiSkill.id == sid)
                # 不碰 updated_at：这一列是内容变更的标记，Home 的"最近更新"读它。
                # 一次统计口径的迁移把 57 个技能全顶到首页,会把真的更新埋掉。
                .values(downloads=new, updated_at=RpAiSkill.updated_at)
            )
        db.commit()
        print(f"\n已写入 {len(changes)} 行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
