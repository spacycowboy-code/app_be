"""一个组名下所有数字资产的清单 —— skill / agent / tool 拉平成一张表。

**为什么要有这个。** 门户里资产分在两张表:``rp_ai_skill`` 和 ``rp_ai_market_item``。
Skill Market 和 Agent Market 各看一张,所以"RSW 名下一共有什么"这个问题在界面上没有任何
一个地方能回答 —— 得开两个页面、各自筛一次、再自己把两份数加起来。给一个组做资产盘点、
或者核对月报里那个分部门数字时,要的是一张表。

**为什么行的形状是自己定的,不复用两个市场的响应模型。** 那两个模型是给卡片用的,带着
图片、评分分布、示例输出这些盘点根本不看的字段,而偏偏缺 ``source``(资产是人工登记的还是
同步来的)。硬凑一个并集会让两边的模型都被这个第三种用途拖着走。

**下载次数用事件表数,不用 ``rp_ai_skill.downloads`` 那个计数器。** 两者对不上:计数器是
累计值、市场条目根本没有这一列。一列数字在同一张表里有两种含义,比没有这一列更糟 —— 看的
人不会知道该怀疑它。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.category import RpAiCategory
from app.models.download import RpAiSkillDownload
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal

#: 盘点表里 ``kind`` 的取值。skill 来自另一张表,其余三种是 ``item_type``。
SKILL_KIND = "skill"


@dataclass(frozen=True)
class AssetRow:
    """盘点表的一行。字段顺序就是 CSV 的列顺序。"""

    kind: str
    id: str
    name: str
    group: str | None
    group_label: str | None
    owner: str | None
    owner_name: str | None
    owner_department: str | None
    author: str | None
    source: str | None
    version: str | None
    status: str | None
    url: str | None
    downloads: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None



@dataclass
class Inventory:
    """一次盘点的结果。

    只剩 ``rows`` 和组信息:原来还有 ``total`` / ``counts`` / ``owners``,那是给页面上的计数卡
    用的,而页面上不展示这份清单,所以它们一个调用方都没有。
    """

    group: str | None
    group_label: str | None
    rows: list[AssetRow] = field(default_factory=list)





def _people(db: Session, eids: set[str]) -> dict[str, RpAiPortal]:
    """一次取回所有人,而不是每行查一次。

    19 个资产里同一个人可能出现 5 次;逐行查会把一次盘点变成几十次往返。
    键统一大写:``creator`` 和 ``uploader_eid`` 的历史数据大小写混用。
    """
    if not eids:
        return {}
    rows = db.scalars(
        select(RpAiPortal).where(func.upper(RpAiPortal.eid).in_({e.upper() for e in eids}))
    ).all()
    return {r.eid.upper(): r for r in rows}


def _download_counts(db: Session, table, id_col) -> dict[str, int]:
    rows = db.execute(select(id_col, func.count()).group_by(id_col)).all()
    return {str(r[0]): int(r[1]) for r in rows}


def collect_inventory(db: Session, group: str | None = None) -> Inventory:
    """列出 ``group`` 名下的全部资产;``group`` 为空则列全部。

    包含 ``ai-platform``。Agent Market 页面把它藏起来是因为那是给读者的货架,而盘点要的是
    "门户里一共有什么" —— 藏一条会让这张表和数据库对不上,而对不上的清单没人敢用。
    """
    slug = (group or "").strip() or None

    skills = db.scalars(
        select(RpAiSkill).where(RpAiSkill.category_id == slug)
        if slug
        else select(RpAiSkill)
    ).all()
    items = db.scalars(
        select(RpAiMarketItem).where(RpAiMarketItem.category_id == slug)
        if slug
        else select(RpAiMarketItem)
    ).all()

    eids = {s.uploader_eid for s in skills if s.uploader_eid}
    eids |= {i.creator for i in items if i.creator}
    people = _people(db, eids)

    skill_dl = _download_counts(db, RpAiSkillDownload, RpAiSkillDownload.skill_id)
    item_dl = _download_counts(db, RpAiMarketDownload, RpAiMarketDownload.item_id)

    labels = {
        c.id: c.label for c in db.scalars(select(RpAiCategory)).all()
    }

    rows: list[AssetRow] = []
    for s in skills:
        who = people.get((s.uploader_eid or "").upper())
        rows.append(
            AssetRow(
                kind=SKILL_KIND,
                id=s.id,
                name=s.name,
                group=s.category_id,
                group_label=labels.get(s.category_id or "") or s.category,
                owner=s.uploader_eid,
                owner_name=who.display_name if who else None,
                owner_department=who.department if who else None,
                author=s.author,
                # skill 表没有 source 列。外部导入的会填 external_source,门户自己上传的
                # 是空 —— 把空显式写成 portal,免得 CSV 里一列空白让人以为是漏了。
                source=s.external_source or "portal",
                version=s.version,
                status=s.status,
                url=s.external_url,
                downloads=skill_dl.get(s.id, 0),
                created_at=s.created_at,
                updated_at=s.updated_at,
            )
        )
    for i in items:
        who = people.get((i.creator or "").upper())
        rows.append(
            AssetRow(
                kind=i.item_type,
                id=i.id,
                name=i.name,
                group=i.category_id,
                group_label=labels.get(i.category_id or "") or i.category,
                owner=i.creator,
                owner_name=who.display_name if who else None,
                owner_department=who.department if who else None,
                author=i.author,
                source=i.source,
                version=i.version,
                status=i.status,
                # 仓库地址优先:盘点的人要找的是代码在哪,而 service_url 只是个能点开的界面。
                url=i.repo_url or i.service_url,
                downloads=item_dl.get(i.id, 0),
                created_at=i.created_at,
                updated_at=i.updated_at,
            )
        )

    # 按类型再按名字排。默认的表顺序是插入顺序,同一次同步进来的会挤在一起,肉眼核对时很难找。
    rows.sort(key=lambda r: (r.kind, r.name.lower()))
    return Inventory(group=slug, group_label=labels.get(slug or "") if slug else None, rows=rows)
