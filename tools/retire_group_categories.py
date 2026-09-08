"""Refile everything out of a retired group, then optionally delete the group.

Why a second script instead of extending ``refile_market_categories.py``: that one
is deliberately narrow. It only touches ``rp_ai_market_item``, and only rows
parked on a *default* category, because its policy comment says a row filed
somewhere on purpose is a legitimate choice that a bulk job must not overrule.

Both of those are wrong for this job:

* Skills have to move too. ``rp_ai_skill.category_id`` carries a foreign key with
  ``ondelete=SET NULL``, so deleting the category row would silently un-file
  them, and a skill with no group is counted in a market's "All" tab while
  appearing under no tab at all.
* Here the deliberate choices are exactly what has to move. ``cd`` and ``ai-team``
  are being retired: a row filed there on purpose is not a choice being overruled,
  it is a choice whose destination no longer exists.

The two failure modes for the two tables differ, which is the reason the delete is
a separate, guarded step rather than the last line of the refile:

* ``rp_ai_skill.category_id`` -> FK ``SET NULL``: the row survives, un-filed.
* ``rp_ai_market_item.category_id`` -> no FK at all: the row keeps a slug with
  nothing behind it. It shows in no tab and is still counted in "All" — the
  "tabs do not add up" bug this repo has already fixed once, and the version of
  it that is hardest to see.

Dry run by default. Nothing is written without ``--apply``.

    # See what would move
    python tools/retire_group_categories.py

    # Rows with no functional signal in the directory need a decision. The dry
    # run names them; assign them explicitly rather than letting a default pick.
    python tools/retire_group_categories.py --assign EHEXYIX=product-system

    # Write it
    python tools/retire_group_categories.py --assign EHEXYIX=product-system --apply

    # Only once nothing references them any more
    python tools/retire_group_categories.py --retire-categories --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402
from app.models.category import RpAiCategory  # noqa: E402
from app.models.market import RpAiMarketItem  # noqa: E402
from app.models.skill import RpAiSkill  # noqa: E402
from app.models.user import RpAiPortal  # noqa: E402
from app.services.dept_groups import RETIRED_GROUP_IDS, verified_group  # noqa: E402
from app.services.eids import looks_like_signum, normalize_eid  # noqa: E402


class Row:
    """One asset to move, flattened so both tables read the same downstream."""

    def __init__(self, table: str, obj, ident: str, name: str, owner: str | None, why: str):
        self.table = table
        self.obj = obj
        self.ident = ident
        self.name = name
        self.owner = owner
        self.why = why
        self.target: str | None = None
        self.reason: str = ""


def _owner_of_item(item: RpAiMarketItem) -> tuple[str | None, str]:
    """``(eid, how it was found)``.

    ``author`` is consulted only when there is no ``creator``, and only when it is
    shaped like a signum — a display name is shared by more than one person, so
    resolving it would move an asset based on a coin flip.
    """
    if item.creator:
        return normalize_eid(item.creator), "creator"
    if looks_like_signum(item.author):
        return normalize_eid(item.author), "author"
    return None, "无 creator，author 也不是工号形态"


def collect(db, sources: set[str]) -> list[Row]:
    rows: list[Row] = []
    for skill in db.scalars(
        select(RpAiSkill).where(RpAiSkill.category_id.in_(sorted(sources)))
    ).all():
        rows.append(
            Row(
                "rp_ai_skill",
                skill,
                str(skill.id),
                str(skill.name),
                normalize_eid(skill.uploader_eid),
                "uploader_eid",
            )
        )
    for item in db.scalars(
        select(RpAiMarketItem).where(RpAiMarketItem.category_id.in_(sorted(sources)))
    ).all():
        owner, why = _owner_of_item(item)
        rows.append(Row("rp_ai_market_item", item, str(item.id), str(item.name), owner, why))
    return rows


def resolve(db, rows: list[Row], assigned: dict[str, str]) -> None:
    """Fill in ``target``/``reason`` for every row, leaving unresolvable ones ``None``."""
    for row in rows:
        if row.owner and row.owner in assigned:
            row.target = assigned[row.owner]
            row.reason = f"{row.owner} 由 --assign 指定"
            continue
        if not row.owner:
            row.reason = row.why
            continue
        person = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == row.owner))
        if person is None:
            row.reason = f"{row.owner} 在 rp_ai_portal 里没有行"
            continue
        if not person.department:
            row.reason = f"{row.owner} 有行但 department 是空的"
            continue
        group = verified_group(db, person.department)
        if not group:
            row.reason = f"{row.owner} 的部门 {person.department!r} 解不出功能分组"
            continue
        row.target = group
        row.reason = f"{row.why} {row.owner} / {person.department}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-category",
        action="append",
        default=None,
        help="要清空的分组，可重复。默认是 dept_groups.RETIRED_GROUP_IDS 里的全部。",
    )
    parser.add_argument(
        "--assign",
        action="append",
        default=[],
        metavar="EID=SLUG",
        help="给目录里没有功能信号的人显式指定去处。",
    )
    parser.add_argument(
        "--retire-categories",
        action="store_true",
        help="删掉已经没有任何行引用的退役分组。会先自己核一遍。",
    )
    parser.add_argument("--apply", action="store_true", help="真的写库。默认只打印。")
    args = parser.parse_args()

    sources = set(args.from_category or RETIRED_GROUP_IDS)
    assigned: dict[str, str] = {}
    for pair in args.assign:
        if "=" not in pair:
            print(f"--assign 要写成 EID=SLUG，收到的是 {pair!r}")
            return 2
        eid, slug = pair.split("=", 1)
        normalized = normalize_eid(eid)
        if not normalized:
            print(f"--assign 的 EID 是空的: {pair!r}")
            return 2
        assigned[normalized] = slug.strip()

    with SessionLocal() as db:
        # 目标分组必须真的有行,否则搬过去的资产在任何标签下都筛不到。
        for slug in sorted(set(assigned.values())):
            if db.get(RpAiCategory, slug) is None:
                print(f"目标分组 {slug!r} 在 rp_ai_category 里没有行，先建它，否则搬过去筛不到。")
                return 2

        rows = collect(db, sources)
        if not rows:
            print(f"{sorted(sources)} 里已经没有任何行。")
        else:
            resolve(db, rows, assigned)
            movable = [r for r in rows if r.target]
            stuck = [r for r in rows if not r.target]

            print(f"=== {sorted(sources)} 里共 {len(rows)} 行 ===")
            for row in sorted(rows, key=lambda r: (r.table, r.name)):
                arrow = row.target or "??? 不动"
                print(f"  [{row.table:18s}] {row.name[:34]:36s} -> {arrow}")
                print(f"      {row.reason}")

            plan: dict[str, int] = {}
            for row in movable:
                plan[str(row.target)] = plan.get(str(row.target), 0) + 1
            print()
            print("=== 汇总 ===")
            for slug in sorted(plan):
                print(f"    {slug:<18} +{plan[slug]}")
            print(f"    可搬 {len(movable)} 行，留在原处 {len(stuck)} 行")
            if stuck:
                print()
                print("    留在原处的行需要你点名去处（--assign EID=SLUG）：")
                for row in stuck:
                    print(f"      {row.name[:40]:42s} {row.reason}")
                print("    在它们还留着的时候删分组行,就会变成两种坏法之一 —— 见本文件开头。")

            if args.apply:
                for row in movable:
                    label = db.get(RpAiCategory, str(row.target))
                    row.obj.category_id = row.target
                    # 显示名和 id 一起改。只改 id 会让卡片继续印着旧组名,而那看起来
                    # 像"数据没搬干净"而不是"少改了一列"。
                    if label is not None:
                        row.obj.category = label.label
                db.commit()
                print()
                print(f"已写入：{len(movable)} 行。")
            else:
                print()
                print("这是 dry run，一行都没动。加 --apply 才会写。")

        if args.retire_categories:
            print()
            print("=== 删除退役分组 ===")
            for slug in sorted(sources):
                skills_left = db.scalar(
                    select(RpAiSkill.id).where(RpAiSkill.category_id == slug).limit(1)
                )
                items_left = db.scalar(
                    select(RpAiMarketItem.id).where(RpAiMarketItem.category_id == slug).limit(1)
                )
                if skills_left or items_left:
                    print(f"  {slug}: 还有行引用它，不删。")
                    continue
                row = db.get(RpAiCategory, slug)
                if row is None:
                    print(f"  {slug}: 已经没有这一行。")
                    continue
                if args.apply:
                    db.delete(row)
                    db.commit()
                    print(f"  {slug}: 已删除。")
                else:
                    print(f"  {slug}: 会被删除（dry run，没动）。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
