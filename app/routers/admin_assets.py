"""Admin 页面上那个「下载资产清单」按钮的后端。

``GET /api/admin/assets.csv?group=radio-sw`` —— 一个组名下的全部数字资产导成一张表。

**只有导出,没有列表接口。** 页面上不展示这份清单(这是明确的决定),所以一个返回 JSON 的
列表端点不会有任何调用方。留着它就是留一份没人用、没人看、却要跟着数据模型一起维护的代码。

**为什么需要它。** 资产在库里分成两张表:``rp_ai_skill`` 和 ``rp_ai_market_item``。Skill
Market 和 Agent Market 各看一张,所以"RSW 名下一共有什么"这个问题在门户里没有任何一个地方
能回答 —— 拿它做盘点或者核对月报的分部门数字时,要的是一张拉平的表。

**为什么导出的是清单而不是资产内容的打包。** 门户对这批资产大多只存一个地址:RSW 名下 28 个
资产里,``gl-`` 开头(唯一能让门户去取代码包的那种)是 **0 个**。Gerrit 同步来的只有
``repo_url``,radioai 那 13 个 agent 也只有链接。所以"把所有资产打包下载"今天做不到,硬做出来
的包会缺掉大部分内容 —— 一个看起来成功、内容却不全的 zip 比一个明确的"没有"更坏。清单里每行
都带地址,要内容顺着它去上游取。

**谁能用。** 和 Dashboard 同一份名单(``dashboard_eids``)。这张表把工号、姓名、部门和每个人
产出了什么摊在一起,那是月报级别的信息,不是市场页面那种公开货架。

**CSV 带 BOM。** 这张表会被 Excel 打开,而 Windows 上的 Excel 不看 UTF-8 就按本地编码解 ——
中文名字会变成乱码。BOM 是让它认出 UTF-8 的唯一办法,而这个问题在 macOS 上和任何文本编辑器
里都看不出来。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.asset_inventory import collect_inventory
from app.services.eids import require_dashboard_eid

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _gate(request: Request) -> str:
    return require_dashboard_eid(request, "导出资产清单")


#: 表头用中文:这张表的读者是拿它做盘点和对月报的人,不是程序。
CSV_HEADER = [
    "类型",
    "id",
    "名称",
    "分组",
    "分组名",
    "记在谁名下(工号)",
    "姓名",
    "部门",
    "作者署名",
    "来源",
    "版本",
    "状态",
    "地址",
    "下载次数",
    "创建时间",
    "更新时间",
]


def _fmt(value: datetime | None) -> str:
    """时间按库本地时区原样输出,不做转换。

    ``created_at`` 存的是库服务器本地时间(生产是 UTC+2),不是 UTC。当成 UTC 再转一次会整体
    偏两小时,而两小时刚好够把月底那几条挪到上个月 —— 报表对不上就是这么来的。这里只格式化,
    让它和数据库里看到的一致。
    """
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


@router.get("/assets.csv")
def export_assets(
    request: Request,
    group: str | None = Query(None, description="分类 slug;不传则导出全部"),
    db: Session = Depends(get_db),
    _eid: str = Depends(_gate),
) -> StreamingResponse:
    inv = collect_inventory(db, group)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for r in inv.rows:
        writer.writerow(
            [
                r.kind,
                r.id,
                r.name,
                r.group or "",
                r.group_label or "",
                r.owner or "",
                r.owner_name or "",
                r.owner_department or "",
                r.author or "",
                r.source or "",
                r.version or "",
                r.status or "",
                r.url or "",
                r.downloads,
                _fmt(r.created_at),
                _fmt(r.updated_at),
            ]
        )

    # 文件名带上组和日期:这种表会被存下来、发出去,过两周没人记得哪份是哪天导的。
    stamp = datetime.now().strftime("%Y%m%d")
    name = f"assets-{inv.group or 'all'}-{stamp}.csv"
    body = "\ufeff" + buf.getvalue()
    return StreamingResponse(
        io.BytesIO(body.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
