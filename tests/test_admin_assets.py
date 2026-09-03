"""Admin 的资产清单导出:``GET /api/admin/assets.csv``。

这份 CSV 的用途是"拿它和数据库、和月报核对",所以这里钉的重点是**不漏不多**:两张表都要进来、
``ai-platform`` 不许像市场页面那样被藏掉、每一行都要出现在文件里。一张少了几行却看不出来的
清单,比没有这张清单更坏 —— 没有人会知道差的是哪一条。
"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.services.asset_inventory import collect_inventory
from app.services.auth_tokens import create_token

ADMIN = "EZHGMIO"
#: 名单里的第二个人,只为证明端点读的是设置而不是写死 ADMIN。刻意用一个中立工号:
#: 这里放一个真实同事的工号会让测试看起来在断言"这个人应该有权限"。
LISTED = "ELISTED"
OUTSIDER = "ENOBODY"
SECRET = "test-secret-for-admin-assets"
GROUP = "test-inv"


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


@pytest.fixture
def seeded(db):
    """一个干净的组,只放这个测试自己的四种资产。"""
    _cleanup(db)
    db.add(RpAiCategory(id=GROUP, label="Test Inventory"))
    db.add(
        RpAiPortal(
            eid="EINV001",
            display_name="Inv Person",
            department="BNEW RTE RP SWD SW2 Software 2",
        )
    )
    db.commit()
    db.add(
        RpAiSkill(
            id="inv-skill", name="Inv Skill", category_id=GROUP, uploader_eid="EINV001",
            author="Inv Person", status="approved", version="1.0",
        )
    )
    for ident, name, kind, src, url in (
        ("inv-agent", "Inv Agent", "agent", "gerrit-ai-assets", "https://gerrit/x"),
        ("inv-tool", "Inv Tool", "tool", "manual", None),
        ("inv-plat", "Inv Platform", "ai-platform", "manual", None),
    ):
        db.add(
            RpAiMarketItem(
                id=ident, name=name, item_type=kind, service_type="cloud",
                category_id=GROUP, creator="EINV001", author="Inv Person",
                source=src, repo_url=url, status="approved",
            )
        )
    db.commit()
    yield
    _cleanup(db)


def _cleanup(db) -> None:
    for model in (RpAiSkill, RpAiMarketItem):
        for row in db.scalars(select(model).where(model.category_id == GROUP)).all():
            db.delete(row)
    for row in db.scalars(
        select(RpAiMarketDownload).where(RpAiMarketDownload.item_id.like("inv-%"))
    ).all():
        db.delete(row)
    for model, ident in ((RpAiCategory, GROUP), (RpAiPortal, "EINV001")):
        row = db.get(model, ident)
        if row:
            db.delete(row)
    db.commit()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", SECRET)
    monkeypatch.setenv("DASHBOARD_EIDS", f"{ADMIN},{LISTED}")
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _headers(eid: str) -> dict[str, str]:
    """真 JWT。只塞 ``X-User-Eid`` 会被站点级网关按未登录拦掉(401),那样测出来的是
    "有没有登录"而不是"有没有权限"。"""
    token, _ = create_token(eid, display_name=eid.title())
    return {"Authorization": f"Bearer {token}"}


def _rows(client, group: str | None = None) -> list[list[str]]:
    q = f"?group={group}" if group else ""
    r = client.get(f"/api/admin/assets.csv{q}", headers=_headers(ADMIN))
    assert r.status_code == 200
    body = r.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(body)))


# --- 收集 ---------------------------------------------------------------------


def test_both_tables_land_in_one_list(db, seeded) -> None:
    inv = collect_inventory(db, GROUP)
    assert {r.id for r in inv.rows} == {"inv-skill", "inv-agent", "inv-tool", "inv-plat"}
    assert {r.kind for r in inv.rows} == {"skill", "agent", "tool", "ai-platform"}


def test_ai_platform_is_not_hidden(db, seeded) -> None:
    """Agent Market 页面藏 ``ai-platform``,盘点不许藏。

    那个页面是给读者的货架,藏一条是编辑决定。这份清单是用来和数据库核对的,少一条就意味着
    对不上。
    """
    assert any(r.kind == "ai-platform" for r in collect_inventory(db, GROUP).rows)


def test_owner_name_and_department_come_from_ldap(db, seeded) -> None:
    row = next(r for r in collect_inventory(db, GROUP).rows if r.id == "inv-agent")
    assert row.owner_name == "Inv Person"
    assert row.owner_department == "BNEW RTE RP SWD SW2 Software 2"


def test_a_skill_with_no_external_source_reads_portal(db, seeded) -> None:
    """空字符串会让 CSV 那一列是空白,看的人分不清"门户自产"和"忘了填"。"""
    assert next(r for r in collect_inventory(db, GROUP).rows if r.kind == "skill").source == "portal"


def test_downloads_come_from_the_event_table(db, seeded) -> None:
    """``rp_ai_skill.downloads`` 那个累计计数器和市场条目对不上(市场表根本没有这一列)。
    一列数字在同一张表里有两种含义,比没有这一列更糟。"""
    db.add(RpAiMarketDownload(item_id="inv-agent", eid="EX1"))
    db.add(RpAiMarketDownload(item_id="inv-agent", eid="EX2"))
    db.commit()
    rows = {r.id: r.downloads for r in collect_inventory(db, GROUP).rows}
    assert rows["inv-agent"] == 2
    assert rows["inv-tool"] == 0


def test_no_group_means_everything(db, seeded) -> None:
    other = RpAiMarketItem(
        id="inv-other-group", name="Elsewhere", item_type="agent", service_type="cloud",
        category_id="ivc", creator="EINV001", source="manual", status="approved",
    )
    db.add(other)
    db.commit()
    try:
        scoped = {r.id for r in collect_inventory(db, GROUP).rows}
        everything = {r.id for r in collect_inventory(db, None).rows}
        assert "inv-other-group" not in scoped
        assert "inv-other-group" in everything
        assert scoped < everything
    finally:
        db.delete(other)
        db.commit()


# --- 端点 ---------------------------------------------------------------------


def test_export_needs_an_admin(client) -> None:
    r = client.get("/api/admin/assets.csv", headers=_headers(OUTSIDER))
    assert r.status_code == 403


def test_a_listed_eid_is_allowed(client) -> None:
    """名单里的人能导出。

    只证明端点尊重 ``dashboard_eids`` 这个设置 —— ``client`` fixture 用环境变量盖掉了出厂
    默认值,所以这条与 ``config.py`` 里到底写了谁无关。下面那条才是钉出厂名单的。

    （这条原来叫 ``test_ecuiden_is_allowed``,针对当时刚加进名单的那个人。她已于 2026-09-02
    移除,所以改成用一个中立的工号 —— 断言的东西一个字没变。）
    """
    assert client.get("/api/admin/assets.csv", headers=_headers(LISTED)).status_code == 200


def test_the_shipped_default_list_does_not_include_ecuiden(monkeypatch) -> None:
    """不带环境覆盖,看代码里发出去的那份名单。

    生产没有设 ``DASHBOARD_EIDS``(查过容器 env),所以 ``config.py`` 的默认值就是线上实际
    生效的名单 —— 改它才真的改变谁看得见什么。

    **这条原来是反过来的**(``assert "ECUIDEN" in ...``),钉的是"她需要 RSW 同步页所以加进
    名单"那个决定。2026-09-02 按操作者要求移除,所以断言跟着反转。

    保留这条而不是删掉,因为它现在防的是另一件事:这张名单同时守着**逐人使用报告**,而下一个
    收到"给 Tracy 开个同步权限"请求的人,顺手把工号加回这里就等于把每个人的姓名和用量一起给
    出去。真要给同步权限,得先把名单拆成两个(见 ``config.py`` 里那段注释)。
    """
    monkeypatch.delenv("DASHBOARD_EIDS", raising=False)
    get_settings.cache_clear()
    try:
        assert "ECUIDEN" not in get_settings().dashboard_eid_list
        # 顺手钉住其余四个还在,否则"整张名单被清空"也会让上面那条变绿。
        assert get_settings().dashboard_eid_list == ["EZHGMIO", "ADMINMM", "EHUIWUU", "EBAOXUE"]
    finally:
        get_settings.cache_clear()


def test_csv_has_a_bom_so_excel_reads_utf8(client) -> None:
    """没有 BOM 时 Windows 上的 Excel 会把中文名字解成乱码。

    这不是靠"我这里打开是好的"能发现的问题 —— macOS 的 Excel 和所有文本编辑器都正常。
    """
    r = client.get("/api/admin/assets.csv", headers=_headers(ADMIN))
    assert r.content.startswith(b"\xef\xbb\xbf")


def test_csv_is_an_attachment_with_a_dated_name(client) -> None:
    cd = client.get(
        "/api/admin/assets.csv?group=radio-sw", headers=_headers(ADMIN)
    ).headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert "assets-radio-sw-" in cd and cd.endswith('.csv"')


def test_every_collected_row_reaches_the_file(client, db, seeded) -> None:
    """收集到 4 条、文件里只有 3 行 —— 这种不一致没有任何提示。"""
    expected = collect_inventory(db, GROUP).rows
    rows = _rows(client, GROUP)
    assert len(rows) - 1 == len(expected)  # 减掉表头
    assert {r[1] for r in rows[1:]} == {a.id for a in expected}


def test_the_row_carries_the_columns_people_came_for(client, db, seeded) -> None:
    rows = _rows(client, GROUP)
    header, body = rows[0], rows[1:]
    line = next(r for r in body if r[1] == "inv-agent")
    got = dict(zip(header, line))
    assert got["类型"] == "agent"
    assert got["名称"] == "Inv Agent"
    assert got["分组"] == GROUP
    assert got["分组名"] == "Test Inventory"
    assert got["记在谁名下(工号)"] == "EINV001"
    assert got["姓名"] == "Inv Person"
    assert got["部门"] == "BNEW RTE RP SWD SW2 Software 2"
    assert got["来源"] == "gerrit-ai-assets"
    assert got["地址"] == "https://gerrit/x"


def test_time_is_written_as_stored_not_shifted(client, db, seeded) -> None:
    """``created_at`` 存的是库服务器本地时间,不是 UTC。

    当成 UTC 再转一次会整体偏两小时(生产库是 UTC+2),而两小时刚好够把月底那几条挪到上个月。
    所以这里断言导出的字符串和库里那个 naive 时间逐字一致。
    """
    row = next(r for r in collect_inventory(db, GROUP).rows if r.id == "inv-agent")
    line = next(r for r in _rows(client, GROUP)[1:] if r[1] == "inv-agent")
    assert line[14] == row.created_at.strftime("%Y-%m-%d %H:%M:%S")


def test_there_is_no_json_listing_endpoint(client) -> None:
    """页面上不展示这份清单,所以没有列表端点。

    钉住这件事是因为"顺手加一个 JSON 版本"很容易发生,而那会变成一份没人调用、却要跟着数据
    模型一起维护的代码。真需要时再加,并且那时它得有自己的调用方。
    """
    assert client.get("/api/admin/assets", headers=_headers(ADMIN)).status_code == 404
