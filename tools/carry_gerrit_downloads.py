"""把 Gerrit 那批资产的下载记录搬到迁移后的新条目上。

迁移之后同一个资产在库里有两行:旧的 ``gerrit-*`` 和新的(市场侧 ``gl-<项目id>``、技能侧门户
生成的 id)。下载记录挂在旧 id 上,而旧行是要删的 —— 两张下载事件表的外键都是
``ondelete=CASCADE``,直接删旧行等于把这些记录一起删掉。

所以顺序是:**先改指向,再删旧行**。

**改指向而不是删了重插。** ``created_at`` 与下载人的工号是月报的取数依据(``figures.py`` 按这
两列算周活与逐人下载),重插会把时间戳换成今天、把"谁下的"这件事重写一遍。UPDATE 只动一列。

技能侧还多一格:``rp_ai_skill.downloads`` 是个累计计数列(与事件表并存,卡片显示读它),
所以除了改事件行还要把这个值加到新行上 —— 只改事件表的话卡片上仍然是 0。

用法(在后端容器里跑):

    python -m tools.carry_gerrit_downloads --map /tmp/gerrit-migration-map.json
    python -m tools.carry_gerrit_downloads --map /tmp/gerrit-migration-map.json --apply

默认空跑。空跑会把每条要动的行列出来,并**核对新行真的存在** —— 外键指向一个不存在的 id 会
让 UPDATE 直接失败,而那时前一半可能已经提交了。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import func, select, update

from app.database import SessionLocal
from app.models.download import RpAiSkillDownload
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.skill import RpAiSkill


def _int(raw: object) -> int:
    """``downloads`` 是 VARCHAR 列,历史上存过非数字。解不出就当 0,不让它带走整批。"""
    try:
        return int(str(raw or "0").strip() or "0")
    except ValueError:
        return 0


def run(*, mapping: dict[str, str], apply: bool) -> int:
    db = SessionLocal()
    problems: list[str] = []
    moves: list[tuple[str, str, str, int, int]] = []   # kind, old, new, 事件数, 计数列

    for old_id, new_id in sorted(mapping.items()):
        if not new_id:
            problems.append(f"{old_id}: 对应表里新 id 是空的")
            continue
        if old_id.startswith("gerrit-skill-"):
            old = db.get(RpAiSkill, old_id)
            new = db.get(RpAiSkill, new_id)
            if old is None or new is None:
                problems.append(
                    f"{old_id} -> {new_id}: "
                    f"{'旧行' if old is None else '新行'}不在库里,跳过"
                )
                continue
            n = db.scalar(
                select(func.count(RpAiSkillDownload.id))
                .where(RpAiSkillDownload.skill_id == old_id)
            ) or 0
            moves.append(("skill", old_id, new_id, n, _int(old.downloads)))
        else:
            old_m = db.get(RpAiMarketItem, old_id)
            new_m = db.get(RpAiMarketItem, new_id)
            if old_m is None or new_m is None:
                problems.append(
                    f"{old_id} -> {new_id}: "
                    f"{'旧行' if old_m is None else '新行'}不在库里,跳过"
                )
                continue
            n = db.scalar(
                select(func.count(RpAiMarketDownload.id))
                .where(RpAiMarketDownload.item_id == old_id)
            ) or 0
            moves.append(("market", old_id, new_id, n, 0))

    print(f"模式 = {'APPLY(真写)' if apply else 'DRY-RUN(不写)'}")
    print()
    print(f"{'侧':<7} {'旧 id':<38} {'新 id':<26} {'事件行':>6} {'计数列':>6}")
    print("-" * 92)
    for kind, old_id, new_id, n, cnt in moves:
        print(f"{kind:<7} {old_id:<38} {new_id:<26} {n:>6} {cnt:>6}")
    total_events = sum(m[3] for m in moves)
    total_counter = sum(m[4] for m in moves)
    print()
    print(f"合计:{len(moves)} 个资产、{total_events} 条下载事件、计数列共 {total_counter}")

    if problems:
        print("\n有问题的(不动它们):")
        for p in problems:
            print(f"  {p}")

    if not apply:
        print("\n空跑结束,一个字节都没动。加 --apply 才写。")
        return 0

    for kind, old_id, new_id, n, cnt in moves:
        if kind == "skill":
            db.execute(
                update(RpAiSkillDownload)
                .where(RpAiSkillDownload.skill_id == old_id)
                .values(skill_id=new_id)
            )
            new = db.get(RpAiSkill, new_id)
            if new is not None and cnt:
                # 加而不是覆盖:新行自己可能已经有下载(迁完到搬完之间有人点过)。
                new.downloads = str(_int(new.downloads) + cnt)
        else:
            db.execute(
                update(RpAiMarketDownload)
                .where(RpAiMarketDownload.item_id == old_id)
                .values(item_id=new_id)
            )
    db.commit()

    print("\n--- 写完之后复核 ---")
    left_m = db.scalar(
        select(func.count(RpAiMarketDownload.id)).where(
            RpAiMarketDownload.item_id.in_([m[1] for m in moves if m[0] == "market"] or [""])
        )
    ) or 0
    left_s = db.scalar(
        select(func.count(RpAiSkillDownload.id)).where(
            RpAiSkillDownload.skill_id.in_([m[1] for m in moves if m[0] == "skill"] or [""])
        )
    ) or 0
    got_m = db.scalar(
        select(func.count(RpAiMarketDownload.id)).where(
            RpAiMarketDownload.item_id.in_([m[2] for m in moves if m[0] == "market"] or [""])
        )
    ) or 0
    got_s = db.scalar(
        select(func.count(RpAiSkillDownload.id)).where(
            RpAiSkillDownload.skill_id.in_([m[2] for m in moves if m[0] == "skill"] or [""])
        )
    ) or 0
    print(f"  旧 id 上还剩:市场 {left_m} / 技能 {left_s}  (都该是 0)")
    print(f"  新 id 上现有:市场 {got_m} / 技能 {got_s}")
    ok = left_m == 0 and left_s == 0 and (got_m + got_s) >= total_events
    print("  判定:" + ("对得上,可以去删旧行了" if ok else "对不上,先别删旧行"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ or "")
    ap.add_argument("--map", type=Path, required=True,
                    help="migrate_gerrit_assets.py --map 写出的 旧 id -> 新 id 对应")
    ap.add_argument("--apply", action="store_true", help="真的写。不给就是空跑。")
    args = ap.parse_args(argv)
    if not args.map.exists():
        print(f"找不到对应表 {args.map}", file=sys.stderr)
        return 2
    mapping = json.loads(args.map.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or not mapping:
        print("对应表是空的或格式不对", file=sys.stderr)
        return 2
    return run(mapping=mapping, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
