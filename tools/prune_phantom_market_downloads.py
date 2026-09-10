"""删掉「点了下载但其实没有包可下」留下的那些流水行。默认 dry run。

## 这些行是怎么来的

``POST /api/market/{id}/download`` 原先的顺序是：先往 ``rp_ai_market_download`` 写一行并
commit，**然后**才判断这个条目的 id 是不是 ``gl-`` 开头（只有 GitLab 同步来的条目能取到打包）。
不是的话返回 409「该项目不是从 GitLab 同步来的，没有可下载的代码包」。

于是在一个只有链接的登记条目上点一次下载 = 库里多一行「这个人下载过」+ 屏幕上一个报错弹窗。
用户什么都没拿到。这个偏差是永久的、只增不减，而且看不出任何异常：那一行长得和一次真实下载
一模一样。

代码那一侧已经修好（静态前置条件挪到了写流水之前，见 ``routers/market.py``），所以**不会再新增**。
这个脚本处理的是已经记下的历史行。

## 为什么不是一并把 502 那种失败也删掉

记流水在**取包失败之前**是有意的：「大家都下不下来」这件事必须能从计数里看出来。那种失败会好转，
而且它描述的是一次真实的尝试。这里删的只有「这个条目根本没有包」这一类 —— 它永远不会好转。

## 判据

``item_id not like 'gl-%'`` **且** 该条目没有 ``external_url``。第二个条件不能漏：上游托管的
agent（IVC/RVC 索引同步进来的）id 也不是 ``gl-`` 开头，但它们的下载是**通的** —— 那些行是真实
下载，删掉就是在销毁真数据。

## 数字会变小，而且补不回来

删掉之后，受影响那些卡片上的下载人数会下降，月报里对应的数字跟着动。这不是数据丢失 —— 那些行
本来就不该存在 —— 但它是一次不可逆的删除，所以默认 dry run，看清了再 ``--apply``。
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402

#: 候选行：条目 id 不是 gl- 开头，且那个条目没有外链地址（或条目已经不存在）。
#: LEFT JOIN 而不是 INNER：条目被删掉后流水会被外键级联清掉，所以理论上不会有孤儿行，但真有的话
#: 它同样是一次下不到任何东西的点击，该一起删。
_CANDIDATES = """
    select d.id, d.item_id, d.eid, d.created_at,
           coalesce(i.name, '(条目已删)') as name,
           i.source, i.item_type
    from rp_ai_market_download d
    left join rp_ai_market_item i on i.id = d.item_id
    where d.item_id not like 'gl-%'
      and (i.id is null or i.external_url is null or i.external_url = '')
    order by d.item_id, d.created_at
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true", help="真的删；不加就只打印将要删什么"
    )
    args = ap.parse_args()

    with SessionLocal() as db:
        total = db.execute(text("select count(*) from rp_ai_market_download")).scalar()
        rows = db.execute(text(_CANDIDATES)).all()

        if not rows:
            print(f"流水共 {total} 行，没有需要清理的 —— 无事可做。")
            return 0

        # 按条目摊开。卡片上显示的是去重人数，所以两个数都要给：删 5 行可能只让卡片少 1。
        by_item: dict[str, dict] = {}
        for _rid, item_id, eid, _at, name, source, item_type in rows:
            slot = by_item.setdefault(
                item_id,
                {"name": name, "source": source, "type": item_type, "rows": 0, "eids": set()},
            )
            slot["rows"] += 1
            if eid:
                slot["eids"].add(eid)

        print(f"流水共 {total} 行，其中 {len(rows)} 行是「没有包可下」留下的，"
              f"涉及 {len(by_item)} 个条目：\n")
        print(f"  {'条目 id':<28} {'名字':<30} {'来源':<14} 删除行数  卡片人数变化")
        for item_id, s in sorted(by_item.items(), key=lambda kv: -len(kv[1]["eids"])):
            print(f"  {item_id:<28} {str(s['name'])[:28]:<30} {str(s['source']):<14} "
                  f"{s['rows']:>7}  {len(s['eids']):>6} -> 0")

        if not args.apply:
            print("\n（dry run，一行都没删。确认无误后加 --apply。）")
            return 0

        ids = [r[0] for r in rows]
        # 分批删而不是一条 IN (…几千个 id…)：MySQL 对超长语句有上限，而那个失败发生在删了一部分
        # 之后，留下一个说不清删到哪儿的中间状态。
        deleted = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            placeholders = ", ".join(f":i{n}" for n in range(len(chunk)))
            params = {f"i{n}": v for n, v in enumerate(chunk)}
            res = db.execute(
                text(f"delete from rp_ai_market_download where id in ({placeholders})"),
                params,
            )
            deleted += res.rowcount or 0
        db.commit()
        print(f"\n已删除 {deleted} 行。剩余流水 "
              f"{db.execute(text('select count(*) from rp_ai_market_download')).scalar()} 行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
