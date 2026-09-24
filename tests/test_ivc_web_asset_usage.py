"""IVC 的 web 型资产：登记它，然后把访问量推进来。

这两件事是一对，所以钉在同一个文件里：``rp_ai_usage_event`` 的落点指向
``rp_ai_market_item``，父行不存在时推来的 ``open`` 只能 skipped。分成两个文件写，
「先登记才能落地」这条链就没有任何一个用例覆盖。

这里钉住五件容易在以后被放宽的事：

1. ``open`` 落 **门户原生的** ``rp_ai_usage_event``（``verb=tool_open``），不是那张外部流水表
   的业务侧。走错表的话，部门页和月报读不到它，而没有任何一处会报错。
2. ``created_at`` 是**对方给的** ``occurredAt``，不是入库时刻。用 ``now()`` 会把一整夜的事件
   全戳在批处理那一刻，而报告是滚动窗口，边界上的归属会错。
3. ``source=nexus-ivc``，与门户自己记的那批分得开 —— 否则"这个数里有多少是上游推来的"答不出来。
4. ``open`` + ``skill`` 是 **422**，不是 skipped。skipped 的含义是"补了数据就能重推"，而这个
   组合重推多少次也落不了地，标成 skipped 会让对方一直重试。
5. ``serviceUrl`` 只收 http/https。这个值会进卡片链接、``embeddable`` 时还进 iframe 的 src，
   而这个端点是机器入口、中间没有人看一眼。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal, db_utc_offset
from app.main import AUTH_WHITELIST, app
from app.models.category import RpAiCategory
from app.models.external_usage_event import RpAiExternalUsageEvent
from app.models.market import RpAiMarketItem
from app.models.usage_event import RpAiUsageEvent
from app.routers import external_usage

ASSET_PATH = "/api/integrations/ivc/web-assets"
USAGE_PATH = "/api/integrations/ivc/usage/events"
#: 两个端点共用这一个 token（2026-09-24 定的，理由与代价见 ``routers/ivc_web_assets`` 的
#: 模块 docstring）。写成一个常量而不是两个同值的,是为了让"哪天又分开"只改一处。
PUSH_TOKEN = "ivc-push-test-secret"

#: 上游身份用 IVC 真实的形状:一个 workspace UUID。刻意不用一段可读 slug —— 那样
#: "门户主键不是从 externalId 拼出来的"这件事在断言上看不出差别,而它正是这一版的核心改动。
UPSTREAM_ID = "6301963c-321b-4546-acfc-95406f5c9cae"
#: 门户主键是**显示名的 slug**(与人工登记路径同一套规则),不带 ``ivc-`` 前缀、和上游 id 无关。
PORTAL_ID = "replir-allocation"
SERVICE_URL = (
    f"https://ivc.internal.example/idea-hub/explore/workspace/{UPSTREAM_ID}?mode=agent"
)

EID = "EZHGMIO"
DEPT = "BNEW RTE RP IVC T4 Team 4"

#: 刻意用一个过去的时刻，好让"created_at 用的是这个、不是 now()"可判定。
OCCURRED = datetime(2026, 9, 1, 14, 22, 31, tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def client(monkeypatch):
    # 站点级 JWT 开着，才能证明两条白名单确实生效。
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", "irrelevant-here")
    monkeypatch.setenv("IVC_PUSH_TOKEN", PUSH_TOKEN)
    get_settings.cache_clear()
    monkeypatch.setattr(
        external_usage, "_departments_for", lambda eids: {eid: DEPT for eid in eids}
    )
    yield TestClient(app)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_ldap(monkeypatch):
    """测试环境没有 LDAP。登记那条路按批开一条连接，这里换成固定答案。"""
    from app.routers import ivc_web_assets

    class _Fixed:
        def department_of(self, eid):  # noqa: D102
            return DEPT

        def close(self):  # noqa: D102
            return None

    monkeypatch.setattr(ivc_web_assets, "DirectoryDepartments", _Fixed)


@pytest.fixture(autouse=True)
def _ivc_category():
    """``ivc`` 分组。登记时会校验它存在（照 ``ivc_sync`` 的先例）。"""
    with SessionLocal() as db:
        if db.get(RpAiCategory, "ivc") is None:
            db.add(RpAiCategory(id="ivc", label="IVC"))
            db.commit()
    yield


@pytest.fixture(autouse=True)
def _clean():
    yield
    with SessionLocal() as db:
        db.query(RpAiUsageEvent).filter(
            RpAiUsageEvent.object_id == PORTAL_ID
        ).delete(synchronize_session=False)
        db.query(RpAiExternalUsageEvent).filter(
            RpAiExternalUsageEvent.asset_id == UPSTREAM_ID
        ).delete(synchronize_session=False)
        # 按 external_ref 清,不只按主键 —— 认领用例造的行主键各不相同(lab-dashboard、
        # hwd-chatbot…),漏掉它们会让下一条用例的 UUID 查询命中上一条的残留。
        db.query(RpAiMarketItem).filter(
            RpAiMarketItem.external_ref == UPSTREAM_ID
        ).delete(synchronize_session=False)
        row = db.get(RpAiMarketItem, PORTAL_ID)
        if row:
            db.delete(row)
        db.commit()


def _asset(**over):
    body = {
        "externalId": UPSTREAM_ID,
        "kind": "agent",
        "name": "ReplIR Allocation",
        "serviceUrl": SERVICE_URL,
        "description": "分配辅助",
        "tags": ["allocation"],
        "version": "1.2.0",
        "author": EID,
    }
    body.update(over)
    return body


def _register(client, **over):
    return client.post(
        ASSET_PATH,
        json={"assets": [_asset(**over)]},
        headers={"Authorization": f"Bearer {PUSH_TOKEN}"},
    )


def _push_open(client, event_id="ivc-1", kind="agent", asset_id=UPSTREAM_ID):
    return client.post(
        USAGE_PATH,
        json={"events": [{
            "eventId": event_id,
            "verb": "open",
            "kind": kind,
            "assetId": asset_id,
            "eid": EID,
            "occurredAt": OCCURRED.isoformat(),
        }]},
        headers={"Authorization": f"Bearer {PUSH_TOKEN}"},
    )


# --- 白名单 ------------------------------------------------------------------

def test_both_machine_endpoints_are_whitelisted():
    """不在白名单里，站点级鉴权会在路由之前答 401，症状是「端点好像不存在」。"""
    assert ASSET_PATH in AUTH_WHITELIST
    assert USAGE_PATH in AUTH_WHITELIST


# --- 登记 --------------------------------------------------------------------

def test_registering_creates_a_cloud_item(client):
    resp = _register(client)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["created"] == 1 and body["skipped"] == 0
    assert body["outcomes"][0]["id"] == PORTAL_ID

    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, PORTAL_ID)
        assert item is not None
        assert item.item_type == "agent"
        # web 型资产的定义就在这一列上。写成 code 会让卡片去展示一个不存在的下载。
        assert item.service_type == "cloud"
        assert item.service_url == SERVICE_URL
        # 两列都写，库里才认得出这行是 IVC 的 —— 生产那 3 行手工建的就是因为缺它们而认不出。
        assert item.source == "nexus-ivc"
        assert item.external_source == "nexus-ivc"
        # 内容在对方那边：这两个必须空，否则下载路径会去找一个不存在的东西。
        assert item.repo_url is None
        assert item.external_url is None
        assert item.status == "approved"


def test_registering_twice_is_unchanged_not_duplicated(client):
    assert _register(client).json()["created"] == 1
    second = _register(client).json()
    assert second["created"] == 0 and second["unchanged"] == 1


def _manual_row(db, *, item_id=PORTAL_ID, name="手工建的", url=SERVICE_URL, kind="agent"):
    """造一行人在界面上手工建的条目：``source=manual``、``external_ref`` 为空。"""
    db.add(RpAiMarketItem(
        id=item_id, name=name, item_type=kind,
        service_type="cloud", source="manual", category_id="ivc",
        status="approved", service_url=url, description="人写的描述",
    ))
    db.commit()


def test_a_manual_row_is_claimed_by_workspace_uuid(client):
    """人手工建的行靠 ``serviceUrl`` 里的 workspace UUID 被认领，并且**显式报出来**。

    这是历史那 12 行唯一可用的对应键:门户主键是显示名的 slug、和上游 id 无关,而按名字对不上
    （门户「IVC Auto-Recovery Agent」在 IVC 侧叫「Recover Workflow」）。
    """
    with SessionLocal() as db:
        _manual_row(db, item_id="lab-dashboard", name="Lab-Dashboard")

    body = _register(client).json()
    assert body["claimed"] == 1, body
    assert body["created"] == 0
    outcome = body["outcomes"][0]
    assert outcome["id"] == "lab-dashboard"
    assert "认领" in outcome["detail"]

    with SessionLocal() as db:
        row = db.get(RpAiMarketItem, "lab-dashboard")
        assert row.external_ref == UPSTREAM_ID
        assert row.external_source == "nexus-ivc"
        assert row.source == "nexus-ivc"


def test_claiming_keeps_the_human_authored_name_and_description(client):
    """认领只建立身份。显示名是用户在市场页上认卡片的依据，覆盖了就找不回来。

    生产上这不是假设:门户「IVC Auto-Recovery Agent」对 IVC 的「Recover Workflow」、门户
    「CIRC(…) RAG」对 IVC 的「CSR」。按登记方给的名字覆盖，用户再也搜不到那张卡片，而
    ``description`` 是 Text、原文没有备份。
    """
    with SessionLocal() as db:
        _manual_row(db, item_id="lab-dashboard", name="Lab-Dashboard")

    _register(client)

    with SessionLocal() as db:
        row = db.get(RpAiMarketItem, "lab-dashboard")
        assert row.name == "Lab-Dashboard"
        assert row.description == "人写的描述"


def test_claiming_does_not_move_the_row_to_the_ivc_group(client):
    """分组决定部门统计把这个资产算给谁，认领不能改它。

    ``hwd-chatbot`` 跑在 IVC 平台上但归属 RD2，门户里在 ``radio-design-2``。按登记方给的分组
    改，会把 RD2 的资产算到 IVC 头上，而部门页和月报都读这一列。
    """
    with SessionLocal() as db:
        if db.get(RpAiCategory, "radio-design-2") is None:
            db.add(RpAiCategory(id="radio-design-2", label="Radio Design 2"))
            db.commit()
        _manual_row(db, item_id="hwd-chatbot", name="HWD Chatbot")
        db.query(RpAiMarketItem).filter(
            RpAiMarketItem.id == "hwd-chatbot"
        ).update({"category_id": "radio-design-2"}, synchronize_session=False)
        db.commit()

    _register(client)

    with SessionLocal() as db:
        assert db.get(RpAiMarketItem, "hwd-chatbot").category_id == "radio-design-2"
        db.query(RpAiMarketItem).filter(
            RpAiMarketItem.id == "hwd-chatbot"
        ).delete(synchronize_session=False)
        db.commit()


def test_claiming_does_fix_a_wrong_service_url(client):
    """服务地址是认领的例外，允许覆盖。

    生产上有 4 行贴的是 staging 环境的 workspace 链接、甚至是 IVC 的建器页面，用户点进去到的
    不是那个 agent。对方是自己服务地址的权威，这一列不覆盖等于把一个坏链接永久保留。
    """
    stale = (
        "https://rvc-staging.internal.example/idea-hub/explore/workspace/"
        f"{UPSTREAM_ID}?mode=agent&conversationId=e110995c"
    )
    with SessionLocal() as db:
        _manual_row(db, item_id="lab-dashboard", name="Lab-Dashboard", url=stale)

    assert _register(client).json()["claimed"] == 1

    with SessionLocal() as db:
        assert db.get(RpAiMarketItem, "lab-dashboard").service_url == SERVICE_URL


def test_claiming_is_idempotent_and_then_reads_as_unchanged(client):
    """认领一次之后走 ``external_ref``，不会再认领第二次、也不会新建。"""
    with SessionLocal() as db:
        _manual_row(db, item_id="lab-dashboard", name="Lab-Dashboard")

    assert _register(client).json()["claimed"] == 1
    second = _register(client).json()
    assert second["claimed"] == 0 and second["created"] == 0
    assert second["updated"] + second["unchanged"] == 1

    with SessionLocal() as db:
        rows = db.scalars(
            select(RpAiMarketItem).where(RpAiMarketItem.external_ref == UPSTREAM_ID)
        ).all()
        assert len(rows) == 1


def test_a_row_without_a_workspace_uuid_is_not_claimed(client):
    """抽不到 UUID 就不猜。名字相同不足以认领 —— 误合两个资产是不可逆的。"""
    with SessionLocal() as db:
        _manual_row(
            db,
            item_id="replir-allocation-old",
            name="ReplIR Allocation",
            url="https://ivc.internal.example/idea-hub/studio/ai-agent-builder",
        )

    body = _register(client).json()
    assert body["created"] == 1 and body["claimed"] == 0

    with SessionLocal() as db:
        assert db.get(RpAiMarketItem, "replir-allocation-old").source == "manual"
        db.query(RpAiMarketItem).filter(
            RpAiMarketItem.id == "replir-allocation-old"
        ).delete(synchronize_session=False)
        db.commit()


def test_a_non_http_service_url_is_rejected(client):
    resp = _register(client, serviceUrl="javascript:alert(1)")
    assert resp.status_code == 422, resp.text


def test_an_external_id_with_a_control_character_is_rejected(client):
    """id 会被原样回显进 ``outcomes`` 并写进日志。

    带 ``\\n`` 的 id 会把一行日志断成两行，排查时看起来像两条不同的记录。它不再被拼成主键，
    所以路径字符不必拦 —— IVC 的 id 是 UUID，本来就不含它们。
    """
    resp = _register(client, externalId="replir\nallocation")
    assert resp.status_code == 422, resp.text


def test_embeddable_from_the_pusher_is_not_applied(client):
    """内嵌是把对方的页面放进门户的同源框架里，是个钓鱼面，该由人决定。

    今天前端还没按这个字段渲染 iframe，但那个功能一做起来，批量推进来的 true 会一夜之间全部
    生效，而没有人为此做过决定。
    """
    assert _register(client, embeddable=True).json()["created"] == 1
    with SessionLocal() as db:
        assert db.get(RpAiMarketItem, PORTAL_ID).embeddable is False


def test_the_primary_key_is_the_name_slug_not_the_upstream_id(client):
    """主键只为可读性服务，身份由 ``external_ref`` 承载。

    拼 ``ivc-<externalId>`` 会造出 ``ivc-6301963c-321b-4546-acfc-95406f5c9cae`` —— 一个人读不懂
    的主键，而它直接出现在市场的前端路由里。
    """
    assert _register(client).json()["outcomes"][0]["id"] == PORTAL_ID
    with SessionLocal() as db:
        row = db.get(RpAiMarketItem, PORTAL_ID)
        assert row.external_ref == UPSTREAM_ID
        assert UPSTREAM_ID not in row.id


def test_a_closed_category_is_refused(client):
    """``official`` 表示门户审过，不该由一个 token 认证的机器入口自己授予。"""
    body = _register(client, categoryId="official").json()
    assert body["skipped"] == 1


def test_ai_platform_is_not_an_accepted_kind(client):
    """前端把 ai-platform 过滤掉不显示，收下来只会造一张谁都看不到的卡片。"""
    resp = _register(client, kind="ai-platform")
    assert resp.status_code == 422, resp.text


def test_a_missing_category_is_skipped_not_500(client):
    body = _register(client, categoryId="no-such-group").json()
    assert body["skipped"] == 1
    assert "不存在" in body["outcomes"][0]["detail"]


def test_a_wrong_token_is_401(client):
    resp = client.post(
        ASSET_PATH, json={"assets": [_asset()]},
        headers={"Authorization": "Bearer not-the-token"},
    )
    assert resp.status_code == 401


def test_the_same_token_opens_both_endpoints(client):
    """两个端点共用 ``ivc_push_token``（2026-09-24 的决定）。

    钉住它是因为这**不是**仓里的默认倾向：推用量和读用量是刻意分开的两个 token。这一条同时
    是那个决定的记录 —— 哪天要把登记分出去，它会变红，改它的人就会看到上面这段理由。
    """
    assert _register(client).status_code == 202
    assert _push_open(client).status_code == 202


def test_asset_endpoint_is_503_when_token_unset(client, monkeypatch):
    """忘配时必须关闭而不是放行。「忘配就全开」的症状是没有症状。"""
    monkeypatch.setenv("IVC_PUSH_TOKEN", "")
    get_settings.cache_clear()
    resp = _register(client)
    assert resp.status_code == 503
    assert "IVC_PUSH_TOKEN" in resp.json()["detail"]


# --- 访问量 ------------------------------------------------------------------

def test_open_lands_in_the_portal_usage_table(client):
    """登记之后推 open，落 ``rp_ai_usage_event`` 且三处口径都对。"""
    assert _register(client).json()["created"] == 1

    body = _push_open(client).json()
    assert body["applied"] == 1, body

    with SessionLocal() as db:
        row = db.scalar(
            select(RpAiUsageEvent).where(RpAiUsageEvent.object_id == PORTAL_ID)
        )
        assert row is not None
        # 走门户原生那张表的既有动词，报告侧不用加分支。
        assert row.verb == "tool_open"
        assert row.object_type == "market_item"
        assert row.object_name == "ReplIR Allocation"
        assert row.eid == EID
        assert row.department == DEPT
        # 与门户自己记的那批分得开。
        assert row.source == "nexus-ivc"
        # 用对方给的时刻，不是入库时刻。差一天就说明走了 server default。
        expected = OCCURRED.astimezone(
            timezone(db_utc_offset())
        ).replace(tzinfo=None)
        assert row.created_at == expected


def test_open_is_also_recorded_in_the_external_ledger(client):
    """幂等靠那张流水表，所以 open 也必须留一行。"""
    _register(client)
    _push_open(client)
    with SessionLocal() as db:
        row = db.scalar(
            select(RpAiExternalUsageEvent).where(
                RpAiExternalUsageEvent.asset_id == UPSTREAM_ID
            )
        )
        assert row is not None
        assert row.verb == "open" and row.kind == "agent"
        assert row.resolved_id == PORTAL_ID
        assert row.action == "applied"


def test_replaying_the_same_open_does_not_double_count(client):
    _register(client)
    assert _push_open(client).json()["applied"] == 1
    assert _push_open(client).json()["duplicate"] == 1
    with SessionLocal() as db:
        rows = db.scalars(
            select(RpAiUsageEvent).where(RpAiUsageEvent.object_id == PORTAL_ID)
        ).all()
        assert len(rows) == 1


def test_open_before_registering_is_skipped_and_says_how_to_fix(client):
    """父行不存在时不能 500，也不能静默丢；要点名补法。"""
    body = _push_open(client).json()
    assert body["skipped"] == 1
    detail = body["outcomes"][0]["detail"]
    assert "web-assets" in detail


def test_open_on_a_skill_is_422_not_skipped(client):
    """技能是下载的，没有"打开"这个动作。这个组合重推多少次也落不了地。"""
    resp = _push_open(client, kind="skill")
    assert resp.status_code == 422, resp.text


def test_open_works_for_a_web_tool_too(client):
    """他要的是 web tool 和 web agent 两种，所以 tool 也得通。"""
    assert _register(client, kind="tool").json()["created"] == 1
    assert _push_open(client, kind="tool").json()["applied"] == 1
    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, PORTAL_ID)
        assert item.item_type == "tool" and item.service_type == "cloud"


# --- 时间 --------------------------------------------------------------------

#: 一个明显早于"今天"的时刻。用它才能判定 created_at 走的是对方给的值、不是 server default。
BORN = datetime(2025, 11, 3, 9, 12, tzinfo=timezone(timedelta(hours=8)))
TOUCHED = datetime(2026, 9, 20, 16, 40, tzinfo=timezone(timedelta(hours=8)))


def _naive(value: datetime) -> datetime:
    return value.astimezone(timezone(db_utc_offset())).replace(tzinfo=None)


def test_created_at_comes_from_the_upstream_not_from_now(client):
    """月报的「本月新增」按这一列计数，所以它必须是资产的出生时间。

    不钉住这条，批量登记一批早就存在的资产会让登记当月的新增凭空多出一批，而没有任何一处
    会报错 —— 数字看起来完全正常。
    """
    assert _register(client, createdAt=BORN.isoformat()).json()["created"] == 1
    with SessionLocal() as db:
        assert db.get(RpAiMarketItem, PORTAL_ID).created_at == _naive(BORN)


def test_a_missing_created_at_is_reported_not_silent(client):
    """退回登记时刻是允许的，但必须说出来。

    一个静默按今天算的创建时间，事后没人分辨得出它是真的今天建的还是补登记的。
    """
    body = _register(client).json()          # 不给 createdAt
    assert body["created"] == 1
    assert "createdAt" in body["outcomes"][0]["detail"]


def test_re_registering_does_not_move_the_birth_date(client):
    """对方每跑一次同步都刷新 created_at 的话，整批资产会被反复算进「本月新增」。"""
    _register(client, createdAt=BORN.isoformat())
    _register(client, createdAt="2026-09-24T10:00:00+08:00")
    with SessionLocal() as db:
        assert db.get(RpAiMarketItem, PORTAL_ID).created_at == _naive(BORN)


def test_updated_at_lands_in_last_activity(client):
    """卡片上的「最后更新」读 ``last_activity``，沿用同步来的条目那一套口径。"""
    _register(client, updatedAt=TOUCHED.isoformat())
    with SessionLocal() as db:
        assert db.get(RpAiMarketItem, PORTAL_ID).last_activity == _naive(
            TOUCHED
        ).isoformat()


def test_a_naive_timestamp_is_refused(client):
    """裸本地时间无法还原成时刻；猜一个偏移会让整批整体偏几小时。"""
    resp = _register(client, createdAt="2025-11-03T09:12:00")
    assert resp.status_code == 422, resp.text


def test_an_epoch_timestamp_is_accepted(client):
    """与 ``usage/events`` 一致，秒级 epoch 也收 —— 共用同一个校验器。"""
    assert _register(client, createdAt=int(BORN.timestamp())).json()["created"] == 1
    with SessionLocal() as db:
        assert db.get(RpAiMarketItem, PORTAL_ID).created_at == _naive(BORN)


def test_a_kind_that_disagrees_with_the_row_is_skipped(client):
    """推 ``tool`` 而门户里登记成 ``agent``：报出来，不静默采纳任意一边。

    ``agent`` 与 ``tool`` 落同一张表，所以这个区别对选表没有作用 —— 它的价值就在这道校验上。
    静默采纳门户那边，对方的报表按 tool 统计、我们按 agent，数字对不上而两边各自都自洽；
    静默采纳对方那边，一次推送就能让这个条目在市场页上换一个 tab，而没有人做过那个决定。
    """
    assert _register(client, kind="agent").json()["created"] == 1

    body = _push_open(client, kind="tool").json()
    assert body["applied"] == 0 and body["skipped"] == 1
    detail = body["outcomes"][0]["detail"]
    # 两边各是什么都要说出来，否则收到这条的人不知道该改哪一处。
    assert "tool" in detail and "agent" in detail and PORTAL_ID in detail

    with SessionLocal() as db:
        # 一条都不许落进访问量。
        assert db.scalars(
            select(RpAiUsageEvent).where(RpAiUsageEvent.object_id == PORTAL_ID)
        ).all() == []
        # 但流水要留，带原因 —— 否则「那批到底收下多少」事后无处可查。
        assert db.scalar(
            select(RpAiExternalUsageEvent).where(
                RpAiExternalUsageEvent.asset_id == UPSTREAM_ID
            )
        ).action == "skipped"


def test_a_matching_kind_still_lands(client):
    """反面对照：上面那条 skipped 是因为类型不一致，不是因为校验把所有 tool 都拦了。"""
    assert _register(client, kind="tool").json()["created"] == 1
    assert _push_open(client, kind="tool").json()["applied"] == 1
