"""One-off: refile market items that were parked in a category by a default.

Dry run is the default.

Why these rows exist
--------------------
The manual registration dialog defaulted ``categoryId`` to ``'public'`` and the
endpoint wrote it through unchecked. ``public`` is a real, selectable category,
so a misfiled row looked completely normal — the author's own filter simply did
not contain their agent. On 2026-09-01 eleven rows sat in ``public``; three of
them were Siyuan Wu's IVC agents, registered minutes apart.

The dialog and the endpoint are fixed in the same change as this file. This
script only moves the rows that were already filed by the old behaviour.

How a row's real group is decided
---------------------------------
The same way the rest of the portal decides it: the LDAP department stored on
the person's portal row, through ``resolve_group``. Nothing here is a judgement
call about what an item "is about" — the name is not evidence. ``IVC-Global-
Knowledge`` reads like IVC, but so does ``Time Report agent`` whose author sits
in ``BNEW RTE RP CD IVC T1``, which is **Chengdu's** IVC team and resolves to
``cd``. Guessing from names is exactly the mistake this script exists to avoid.

Two people, two sources:

* ``creator`` — who pressed the button. Preferred, because it is a signum and it
  is what every report already groups by.
* ``author`` — used only when ``creator`` is NULL (three rows synced or seeded
  before ``creator`` existed as a column). Only accepted when it looks like a
  signum and matches a portal row; a display name like ``Shuo Yang`` is not
  resolved by name, because two people can share one.

A row whose group cannot be resolved is **left alone and reported**, never
guessed and never emptied. Leaving it in ``public`` is visible and reversible;
writing a wrong group is neither.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.category import RpAiCategory  # noqa: E402
from app.models.market import RpAiMarketItem  # noqa: E402
from app.models.user import RpAiPortal  # noqa: E402
from app.services.dept_groups import verified_group  # noqa: E402
from app.services.eids import normalize_eid  # noqa: E402

#: Only rows sitting here are candidates. Deliberately not "any row whose group
#: disagrees with its creator's department": people are moved between
#: departments and an item deliberately filed elsewhere is a legitimate choice.
#: This script is about undoing one specific default, not enforcing a rule.
DEFAULT_PARKED_CATEGORY = "public"


def _department_for(db, eid: str | None) -> tuple[str | None, str | None]:
    """Return ``(department, display_name)`` for a signum, or ``(None, None)``."""
    if not eid:
        return None, None
    row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == normalize_eid(eid)))
    if row is None:
        return None, None
    return row.department, row.display_name


def _looks_like_signum(value: str | None) -> bool:
    """A signum is one alphanumeric word. ``Shuo Yang`` is a name, not a signum."""
    if not value:
        return False
    v = value.strip()
    return v.isalnum() and " " not in v


def resolve_for_item(db, item: RpAiMarketItem) -> tuple[str | None, str]:
    """The group this item should carry, plus how it was decided."""
    dept, name = _department_for(db, item.creator)
    if dept:
        group = verified_group(db, dept)
        if group:
            return group, f"creator {item.creator} ({name or '?'}) / {dept}"
        return None, f"creator {item.creator} 的部门 {dept!r} 解不出分组"

    if item.creator:
        return None, f"creator {item.creator} 在用户表里没有行"

    # No creator: fall back to author, but only if it is a signum.
    if _looks_like_signum(item.author):
        dept, name = _department_for(db, item.author)
        if dept:
            group = verified_group(db, dept)
            if group:
                return group, f"author {item.author} ({name or '?'}) / {dept}"
            return None, f"author {item.author} 的部门 {dept!r} 解不出分组"
        return None, f"author {item.author!r} 在用户表里没有行"

    return None, f"没有 creator,author {item.author!r} 不是工号"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refile market items parked in a default category"
    )
    parser.add_argument(
        "--from-category",
        default=DEFAULT_PARKED_CATEGORY,
        help=f"category to move rows out of (default: {DEFAULT_PARKED_CATEGORY})",
    )
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()

    with SessionLocal() as db:
        items = db.scalars(
            select(RpAiMarketItem)
            .where(RpAiMarketItem.category_id == args.from_category)
            .order_by(RpAiMarketItem.created_at)
        ).all()

        if not items:
            print(f"没有 category_id='{args.from_category}' 的条目,无事可做。")
            return 0

        print(f"category_id='{args.from_category}' 共 {len(items)} 条\n")
        moves: list[tuple[RpAiMarketItem, str]] = []
        skips: list[tuple[RpAiMarketItem, str]] = []
        for item in items:
            group, why = resolve_for_item(db, item)
            hidden = " [ai-platform,agent market 页面不显示]" if (
                item.item_type == "ai-platform"
            ) else ""
            if group:
                print(f"  {item.name[:34]:34} -> {group:15} ({why}){hidden}")
                moves.append((item, group))
            else:
                print(f"  {item.name[:34]:34} -> 保持不动          ({why}){hidden}")
                skips.append((item, why))

        # A target category with no row would file the item somewhere no tab
        # filters by — the very failure this script is undoing.
        missing = sorted(
            {g for _, g in moves if db.get(RpAiCategory, g) is None}
        )
        if missing:
            print(f"\n中止:目标分类在 rp_ai_category 里没有行:{missing}")
            print("先部署带 ensure_group_categories 的版本,或手工建这些行。")
            return 1

        print(f"\n要移动 {len(moves)} 条,保持不动 {len(skips)} 条")
        by_group: dict[str, int] = {}
        for _, g in moves:
            by_group[g] = by_group.get(g, 0) + 1
        for g, n in sorted(by_group.items(), key=lambda kv: -kv[1]):
            print(f"  {g:15} +{n}")

        if not args.apply:
            print("\n这是 dry run,一个字都没写。加 --apply 执行。")
            return 0

        for item, group in moves:
            item.category_id = group
            # ``category`` is the display label shown on the card; leaving the old
            # one would make a card say "Public" while filtering as its new group.
            cat = db.get(RpAiCategory, group)
            item.category = cat.label if cat else None
        db.commit()
        print(f"\n已更新 {len(moves)} 条。")
        print(
            "注意:月报的分部门数字会随之变化(这些资产本来就属于这些组,"
            "不是造数字,但下一份报告的表会和上一份不同)。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
