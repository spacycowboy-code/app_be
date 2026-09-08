"""补 ``rp_ai_portal.department`` 的历史空缺。只写这一列，别的什么都不碰。默认 dry run。

**为什么需要这个脚本，而 MR 里明明写着「跑一次同步就自动补齐」。**

那句话仍然成立，但它的前提是手上有一份**当前的真索引**。门户到 Nexus 整段 TCP 不可达，
所以索引只能由人从工位浏览器贴进来。而 workspace 里存的那份 2026-08-31 样本**不能用**：
逐字段比对发现它的 ``description`` 全是截断成一句话的短版，拿它跑同步会把 17 个 IVC 技能
的描述一起覆盖掉 —— 而描述被改短不报错，只是悄悄和上游不一致。

所以「补部门」从「重新同步」里拆出来单独做：同一个数据来源（目录）、同一条判断
（``DirectoryDepartments``，查不到就留空、绝不换人），但只写那一列。

**为什么它该留在仓里而不是跑完就删。** 建用户行的路径不止一条，而目录里查不到部门的人
（新入职、部门字段还没填）任何时候都可能建出一行空的。``ensure_portal_user`` 会在这个人
下一次经过时补上，但「下一次经过」可能很久，也可能永远不会 —— 那 5 个 IVC 的人就是从未
登录过门户。所以需要一个能随时重跑、幂等、只补空缺的工具。

只补空缺、不覆盖：非空的值可能是这个人自己登录时写下的，比目录快照新。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402
from app.models.market import RpAiMarketItem  # noqa: E402
from app.models.skill import RpAiSkill  # noqa: E402
from app.models.user import RpAiPortal  # noqa: E402
from app.services.asset_owner import DirectoryDepartments  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真的写库。默认只打印。")
    args = ap.parse_args()

    directory = DirectoryDepartments()
    try:
        with SessionLocal() as db:
            blank = db.scalars(
                select(RpAiPortal).where(
                    (RpAiPortal.department.is_(None)) | (RpAiPortal.department == "")
                )
            ).all()

            skills = db.scalars(select(RpAiSkill)).all()
            items = db.scalars(select(RpAiMarketItem)).all()

            def owns(eid: str) -> int:
                n = sum(1 for s in skills if (s.uploader_eid or "").upper() == eid)
                n += sum(1 for i in items if (i.creator or "").upper() == eid)
                return n

            print(f"=== 用户表里 department 为空的行共 {len(blank)} 条 ===")
            filled = 0
            skipped_no_assets = 0
            skipped_not_in_dir = 0
            for row in sorted(blank, key=lambda r: (r.eid or "")):
                eid = (row.eid or "").strip().upper()
                n = owns(eid)
                dept = directory.department_of(eid)
                if dept is None:
                    # 目录里查不到，或目录里也没有部门。留空是显式的空白；编一个值是一个
                    # 事后没人能判断对错的数字。
                    if n:
                        print(f"  - {eid:<10} 名下 {n:>2} 件  目录里没有部门 -> 留空")
                    skipped_not_in_dir += 1
                    continue
                if n == 0:
                    # 名下没有资产的行补了也不影响任何数字（多为测试/机器人行）。不碰，
                    # 免得让一次数据修复的影响面大于它要解决的问题。
                    skipped_no_assets += 1
                    continue
                print(f"  + {eid:<10} 名下 {n:>2} 件  -> {dept}")
                if args.apply:
                    row.department = dept[:64]
                filled += 1

            print()
            print(f"=== 汇总 ===")
            print(f"    会补 {filled} 行（都名下有资产）")
            print(f"    跳过 {skipped_no_assets} 行：名下 0 件资产，补了不影响任何数字")
            print(f"    跳过 {skipped_not_in_dir} 行：目录里查不到或目录里也没部门")

            if args.apply:
                db.commit()
                print()
                print(f"已写入 {filled} 行。")
            else:
                print()
                print("这是 dry run，一行都没动。加 --apply 才会写。")
    finally:
        directory.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
