"""RVC 用量推送端点：``POST /api/integrations/ivc/usage/events``。

这个文件钉的几乎全是**不报错**的失败形状 —— 那也是这个端点唯一真正的风险面。它整批答 202，
对方的实现者只看状态码时，无论门户这边收下了多少都是「成功」。

按危害排序：

1. **重推翻倍。** 我们提交成功后网络超时，对方必然重推。没有幂等键那批下载就翻倍，而翻倍后的
   月报看起来完全正常。这里钉住同一个 ``eventId`` 第二次是 ``duplicate`` 且业务表不动。
2. **时区被丢掉而不是换算。** 对方在 ``+08:00``，库存的是库本地时间（生产 ``+02:00``）。直接
   ``replace(tzinfo=None)`` 会整体偏两小时，而两小时刚好够把月底那几条挪到上个月。这里用
   「同一时刻的两种时区写法必须存成同一个值」来钉，不依赖库的偏移量究竟是多少。
3. **裸本地时间被宽容接受。** 猜一个偏移的代价与上一条相同，而且事后无法分辨。必须 422。
4. **占位动词 ``download/like``。** 契约文档里那份样例长期写的是这个占位符。对方照抄就是每条
   被拒；错误消息里必须带上合法取值，否则一句「unknown verb」帮不上任何忙。
5. **毫秒级 epoch 被当秒读。** 13 位时间戳当秒读会得到公元 5 万年 —— 那种行会静默永远留在
   「最近 7 天」窗口之外，不报错。
6. **一条坏数据回滚整批。** 一个已下架的资产不能挡住同批另外 499 条好数据。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal, db_utc_offset
from app.main import AUTH_WHITELIST, app
from app.models.download import RpAiSkillDownload
from app.models.external_usage_event import RpAiExternalUsageEvent
from app.models.favorite import RpAiUserFavorite
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.routers import external_usage

PATH = "/api/integrations/ivc/usage/events"
TOKEN = "rvc-push-test-secret"

#: 技能侧仍然是 ``ivc-<上游 id>``:那些行是 ``ivc_sync`` 按对方 Nexus 索引里的 id 建的,同一个
#: id 空间,拼前缀必然命中。
SKILL_UPSTREAM_ID = "skill-creator"
SKILL_PORTAL_ID = "ivc-skill-creator"
#: 市场条目侧**没有**对应的门户 id 常量:身份靠 ``external_ref``,主键与上游 id 无关。
#: 写一个 ``AGENT_PORTAL_ID = "ivc-code-reviewer"`` 会把已经废掉的拼前缀口径重新固化进测试。
AGENT_UPSTREAM_ID = "code-reviewer"

EID = "EZHGMIO"
DEPT = "BNEW RTE RP IVC T4 Team 4"


@pytest.fixture
def client(monkeypatch):
    # 站点级 JWT 开着，才能证明白名单那条确实生效。
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", "irrelevant-here")
    monkeypatch.setenv("IVC_PUSH_TOKEN", TOKEN)
    get_settings.cache_clear()
    # 测试环境没有 LDAP。按批查目录那一步在真实部署里是一条连接，这里换成固定答案，
    # 免得每个用例都去等一个连接超时。
    monkeypatch.setattr(
        external_usage, "_departments_for", lambda eids: {eid: DEPT for eid in eids}
    )
    yield TestClient(app)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _assets():
    """一个 IVC 同步来的技能。agent 那条**故意不建** —— 见 test_an_agent_event_is_skipped。"""
    with SessionLocal() as db:
        db.add(
            RpAiSkill(
                id=SKILL_PORTAL_ID,
                name="skill-creator",
                status="approved",
                category_id="public",
                external_url="https://nexus.example/skills/skill-creator",
                external_source="nexus-ivc",
            )
        )
        db.commit()


def _post(client, events, token: str = TOKEN):
    return client.post(
        PATH, json={"events": events}, headers={"Authorization": f"Bearer {token}"}
    )


def _event(**over):
    base = {
        "eventId": "rvc-8891",
        "verb": "download",
        "kind": "skill",
        "assetId": SKILL_UPSTREAM_ID,
        "eid": EID,
        "occurredAt": "2026-09-01T14:22:31+08:00",
    }
    base.update(over)
    return base


# --- 闸门 -------------------------------------------------------------------


def test_the_path_is_whitelisted_so_its_own_token_is_the_only_gate():
    """不在白名单里的话，站点级鉴权会在路由之前答 401，症状是「端点好像不存在」。"""
    assert PATH in AUTH_WHITELIST


def test_without_a_token_it_refuses(client):
    assert client.post(PATH, json={"events": [_event()]}).status_code == 401


def test_a_wrong_token_is_refused(client):
    assert _post(client, [_event()], token="nope").status_code == 401


def test_with_no_token_configured_the_endpoint_is_off_not_open(client, monkeypatch):
    """token 没配时必须 503。放行才是真正的危险：这个路径不在 JWT 闸门后面。"""
    monkeypatch.setenv("IVC_PUSH_TOKEN", "")
    get_settings.cache_clear()
    r = _post(client, [_event()])
    assert r.status_code == 503
    assert "IVC_PUSH_TOKEN" in r.json()["detail"]


# --- 下载 -------------------------------------------------------------------


def test_a_download_lands_in_the_skill_download_table(client):
    """必须落 ``rp_ai_skill_download``，不是通用事件表。

    market 卡片的唯一下载人数和 ``grafana/dashboards/skill-metrics.json`` 都直接查这张表；
    进通用表意味着那些数字看不到 RVC 的量，而不会有任何一处报错。
    """
    r = _post(client, [_event()])
    assert r.status_code == 202, r.text
    body = r.json()
    assert (body["received"], body["applied"], body["skipped"]) == (1, 1, 0)

    with SessionLocal() as db:
        rows = db.scalars(select(RpAiSkillDownload)).all()
        assert len(rows) == 1
        assert rows[0].skill_id == SKILL_PORTAL_ID
        assert rows[0].eid == EID
        # 部门是快照。空着就等于这个人不属于任何部门，他的量进「无法归属」。
        assert rows[0].department == DEPT


def test_the_download_time_is_the_upstream_time_not_now(client):
    r = _post(client, [_event(occurredAt="2026-09-01T14:22:31+08:00")])
    assert r.status_code == 202, r.text
    with SessionLocal() as db:
        stored = db.scalars(select(RpAiSkillDownload)).one().created_at
    # 库里是 naive、库本地时区。贴回偏移后必须还是对方给的那个时刻。
    assert stored.replace(tzinfo=timezone(db_utc_offset())) == datetime.fromisoformat(
        "2026-09-01T14:22:31+08:00"
    )


def test_the_same_instant_in_two_timezones_stores_the_same_value(client):
    """时区是被**换算**的，不是被丢掉的。

    这一条不依赖库的偏移量是多少，所以它在任何部署上都成立。``replace(tzinfo=None)`` 那种写法
    会让这两条差 6 小时。
    """
    r = _post(
        client,
        [
            _event(eventId="a", occurredAt="2026-09-01T14:22:31+08:00"),
            _event(eventId="b", occurredAt="2026-09-01T08:22:31+02:00"),
        ],
    )
    assert r.status_code == 202, r.text
    with SessionLocal() as db:
        times = [row.occurred_at for row in db.scalars(select(RpAiExternalUsageEvent))]
    assert len(times) == 2
    assert times[0] == times[1]


def test_epoch_seconds_are_accepted(client):
    """契约里对时间让过一步：ISO 8601 带偏移**或**秒级 epoch。"""
    r = _post(client, [_event(occurredAt=1_756_702_800)])
    assert r.status_code == 202, r.text
    assert r.json()["applied"] == 1


def test_a_naive_timestamp_is_refused(client):
    """裸本地时间无法还原成时刻。现在是一个 422，宽容下去是一份事后无法分辨对错的报表。"""
    r = _post(client, [_event(occurredAt="2026-09-01T14:22:31")])
    assert r.status_code == 422
    assert "时区" in r.text


def test_millisecond_epoch_is_refused_rather_than_read_as_seconds(client):
    """13 位当秒读会得到公元 5 万年 —— 那行会静默永远留在任何时间窗之外。"""
    r = _post(client, [_event(occurredAt=1_756_702_800_000)])
    assert r.status_code == 422
    assert "epoch" in r.text


# --- 幂等 -------------------------------------------------------------------


def test_the_same_event_id_twice_does_not_double_count(client):
    """危害最大的那一条：翻倍后的月报看起来完全正常。"""
    assert _post(client, [_event()]).json()["applied"] == 1

    second = _post(client, [_event()]).json()
    assert second["applied"] == 0
    assert second["duplicate"] == 1
    assert second["outcomes"][0]["action"] == "duplicate"

    with SessionLocal() as db:
        assert len(db.scalars(select(RpAiSkillDownload)).all()) == 1


def test_two_different_people_downloading_the_same_asset_both_count(client):
    """幂等键是 eventId，不是 (资产, 人)。按人去重是**读取侧** count(distinct eid) 的事。"""
    r = _post(
        client,
        [
            _event(eventId="rvc-1", eid="EZHGMIO"),
            _event(eventId="rvc-2", eid="EPENYAN"),
        ],
    )
    assert r.json()["applied"] == 2
    with SessionLocal() as db:
        assert len(db.scalars(select(RpAiSkillDownload)).all()) == 2


def test_the_same_person_downloading_twice_is_two_rows(client):
    """同一个人下两次是真实信号，不该在写入侧被判重。"""
    r = _post(
        client,
        [
            _event(eventId="rvc-1", occurredAt="2026-09-01T10:00:00+08:00"),
            _event(eventId="rvc-2", occurredAt="2026-09-02T10:00:00+08:00"),
        ],
    )
    assert r.json()["applied"] == 2
    with SessionLocal() as db:
        assert len(db.scalars(select(RpAiSkillDownload)).all()) == 2


# --- 点赞 / 取消 ------------------------------------------------------------


def test_like_then_unlike_leaves_no_favourite_but_keeps_both_in_the_ledger(client):
    """门户自己的收藏功能只有状态，所以永久丢掉了全部取消时间。这里不重演那个坑。"""
    r = _post(
        client,
        [
            _event(eventId="l1", verb="like", occurredAt="2026-09-01T10:00:00+08:00"),
            _event(eventId="u1", verb="unlike", occurredAt="2026-09-01T11:00:00+08:00"),
        ],
    )
    assert r.status_code == 202, r.text
    assert r.json()["applied"] == 2

    with SessionLocal() as db:
        assert db.scalars(select(RpAiUserFavorite)).all() == []
        verbs = sorted(e.verb for e in db.scalars(select(RpAiExternalUsageEvent)))
        assert verbs == ["like", "unlike"]


def test_a_like_creates_the_favourite_row(client):
    r = _post(client, [_event(verb="like")])
    assert r.status_code == 202, r.text
    with SessionLocal() as db:
        row = db.scalars(select(RpAiUserFavorite)).one()
        assert (row.eid, row.skill_id) == (EID, SKILL_PORTAL_ID)


def test_an_unlike_with_nothing_to_remove_is_recorded_not_an_error(client):
    """重放一段历史、或这个人在门户上自己取消过，都会走到这里。流水照落。"""
    r = _post(client, [_event(verb="unlike")])
    assert r.status_code == 202, r.text
    assert "本来就不是收藏状态" in r.json()["outcomes"][0]["detail"]
    with SessionLocal() as db:
        assert db.scalars(select(RpAiExternalUsageEvent)).one().verb == "unlike"


# --- 落不了地的那些 --------------------------------------------------------


def test_an_agent_event_is_skipped_when_the_item_is_not_in_the_portal(client):
    """父行不在时 skipped，并在返回体里点名是哪一行。

    **这条测试原先叫 ``..._because_agents_are_not_synced_yet``，那个理由已经过期**：
    ``ivc_sync._upsert_agent`` 后来补上了，agent 现在能同步进市场（生产已有 2 行
    ``agent/code``），web 型的还能走 ``POST /api/integrations/ivc/web-assets`` 登记。
    所以钉住的不再是"agent 这条路没接"，而是"父行不在就落不了地"——那对任何 kind 都成立。

    不是 4xx —— 门户还没有这个资产是门户这边的欠账，不是对方的请求错。但必须出现在返回体里，
    否则对方看着 202 会以为收下了。
    """
    r = _post(client, [_event(kind="agent", assetId=AGENT_UPSTREAM_ID)])
    assert r.status_code == 202, r.text
    body = r.json()
    assert (body["applied"], body["skipped"]) == (0, 1)
    # 点名的是**上游 id**，不再是拼出来的门户 id：现在还不知道它会落在哪一行，正因为没登记过。
    assert AGENT_UPSTREAM_ID in body["outcomes"][0]["detail"]
    with SessionLocal() as db:
        assert db.scalars(select(RpAiMarketDownload)).all() == []
        # 流水仍要落，且带原因 —— 否则「那批到底收下多少」事后无处可查。
        assert db.scalars(select(RpAiExternalUsageEvent)).one().action == "skipped"


def test_an_agent_event_lands_once_the_item_is_registered(client):
    """反面对照：上面那条 skipped 是因为身份没建立，不是因为 agent 这条路本身没接。

    **身份靠 ``external_ref``，不靠 ``ivc-<assetId>`` 拼主键。** 这里刻意把主键取成一个和上游
    id 毫无关系的值，好让"拼前缀还能不能对上"在断言上能被看见 —— 生产上那 12 行正是这样：
    主键是人工显示名的 slug。
    """
    with SessionLocal() as db:
        db.add(
            RpAiMarketItem(
                id="a-slug-nobody-can-derive",
                name="code-reviewer",
                item_type="agent",
                service_type="code",
                category_id="public",
                external_source="nexus-ivc",
                external_ref=AGENT_UPSTREAM_ID,
            )
        )
        db.commit()

    r = _post(client, [_event(kind="agent", assetId=AGENT_UPSTREAM_ID)])
    assert r.status_code == 202, r.text
    assert r.json()["applied"] == 1
    with SessionLocal() as db:
        got = db.scalars(select(RpAiMarketDownload)).one()
        assert got.item_id == "a-slug-nobody-can-derive"


def test_an_unknown_skill_is_skipped_with_the_portal_id_named(client):
    r = _post(client, [_event(assetId="never-synced")])
    body = r.json()
    assert (body["applied"], body["skipped"]) == (0, 1)
    assert "ivc-never-synced" in body["outcomes"][0]["detail"]


# --- 重推 -------------------------------------------------------------------
#
# 这一组钉的是「skipped 之后补齐前提能重推」。生产上出过一次：2026-09-24 IVC 推来 18 条技能
# 下载/收藏，那 5 个技能当时不在门户里，全部 skipped;9-28 索引同步把它们拉进来了,前提已经
# 满足,可那 18 条再也推不进来 —— 判重按「这个 eventId 见过没有」算,而 skipped 的行也占位。
# 于是 detail 里那三句「…再重推」全是空话,对方照着做还会以为 duplicate 意味着已经收下了。


def _ledger_rows(event_id: str):
    with SessionLocal() as db:
        return db.scalars(
            select(RpAiExternalUsageEvent).where(
                RpAiExternalUsageEvent.event_id == event_id
            )
        ).all()


def test_a_skipped_event_can_be_retried_with_the_same_event_id(client):
    """补齐前提后用**同一个 eventId** 重推 → applied，且业务表真的加了行。

    这是 detail 里那句「先跑一次索引同步」的兑现。钉不住它，那句话就是骗人的。
    """
    first = _post(client, [_event(eventId="retry-1", assetId="late-skill")]).json()
    assert (first["applied"], first["skipped"]) == (0, 1)

    # 前提补上：索引同步把这个技能拉进来了。
    with SessionLocal() as db:
        db.add(
            RpAiSkill(
                id="ivc-late-skill",
                name="late-skill",
                status="approved",
                category_id="public",
                external_url="https://nexus.example/skills/late-skill",
                external_source="nexus-ivc",
            )
        )
        db.commit()

    second = _post(client, [_event(eventId="retry-1", assetId="late-skill")]).json()
    assert (second["applied"], second["duplicate"], second["skipped"]) == (1, 0, 0)

    with SessionLocal() as db:
        got = db.scalars(
            select(RpAiSkillDownload).where(
                RpAiSkillDownload.skill_id == "ivc-late-skill"
            )
        ).all()
        assert len(got) == 1, "重推必须真的写业务表，不只是把返回体改成 applied"


def test_retrying_reuses_the_same_ledger_row(client):
    """一个 eventId 始终只有一行流水，action 从 skipped 改成 applied。

    插新行会撞 ``uq_ext_usage_source_event``，被外层当成 duplicate 接住 —— 那样重推看起来
    成功了，业务表却一个字没改。而两行并存则让「这条事件最终怎么了」需要按时间挑最后一行。
    """
    _post(client, [_event(eventId="retry-2", assetId="late-skill-2")])
    assert [r.action for r in _ledger_rows("retry-2")] == ["skipped"]

    with SessionLocal() as db:
        db.add(
            RpAiSkill(
                id="ivc-late-skill-2",
                name="late-skill-2",
                status="approved",
                category_id="public",
                external_url="https://nexus.example/skills/late-skill-2",
                external_source="nexus-ivc",
            )
        )
        db.commit()

    _post(client, [_event(eventId="retry-2", assetId="late-skill-2")])
    rows = _ledger_rows("retry-2")
    assert len(rows) == 1
    assert rows[0].action == "applied"
    assert rows[0].resolved_id == "ivc-late-skill-2"


def test_an_applied_event_is_still_refused_on_replay(client):
    """放开重推**不能**把真正的幂等一起放开。

    这条是上面两条的反面对照：如果判重条件写成「行不存在就处理」，下载会翻倍，而返回体看起来
    完全正常。
    """
    first = _post(client, [_event(eventId="replay-1")]).json()
    assert first["applied"] == 1

    second = _post(client, [_event(eventId="replay-1")]).json()
    assert (second["applied"], second["duplicate"]) == (0, 1)

    with SessionLocal() as db:
        got = db.scalars(
            select(RpAiSkillDownload).where(
                RpAiSkillDownload.skill_id == SKILL_PORTAL_ID
            )
        ).all()
        assert len(got) == 1, "重放一条已落地的事件不能再写一行下载"


def test_two_skips_in_a_row_keep_one_ledger_row_with_the_latest_reason(client):
    """前提一直没补上时，重推仍然 skipped，流水仍只有一行，原因更新成最新的。"""
    _post(client, [_event(eventId="retry-3", assetId="still-missing")])
    _post(client, [_event(eventId="retry-3", assetId="still-missing")])
    rows = _ledger_rows("retry-3")
    assert len(rows) == 1
    assert rows[0].action == "skipped"
    assert "ivc-still-missing" in (rows[0].detail or "")


def test_the_ledger_has_the_unique_constraint_that_actually_prevents_doubling(client):
    """幂等的**保证**在这道约束上。

    约束被谁删掉时翻倍会静默发生，必须有一条测试立在这里。

    **这条 docstring 原先写着「把 ``_handle_one`` 里那句判重删掉，全部测试照样绿，所以那句只是
    快路径」——那个说法在 2026-09-28 之后不成立了**：判重那一句现在同时决定「插新行还是更新旧
    行」，删掉它会让重推走进插新行那条路、撞上这道约束、被外层报成 duplicate，于是重推静默失效。
    钉住这件事的是 ``test_retrying_reuses_the_same_ledger_row``。
    """
    from app.models.external_usage_event import RpAiExternalUsageEvent as Ledger

    uniques = {
        tuple(c.name for c in arg.columns)
        for arg in Ledger.__table__.constraints
        if arg.__class__.__name__ == "UniqueConstraint"
    }
    assert ("source", "event_id") in uniques


def test_an_event_that_raises_is_isolated_and_the_rest_of_the_batch_persists(
    client, monkeypatch
):
    """SAVEPOINT 的**回滚**那一半，这条钉住它。

    炸之前先写一行，否则这条测试不成立：反面探针证明过，一个「什么都没写就抛」的事件在有没有
    SAVEPOINT 时结果完全一样，那样的测试是装饰。半途写下的那行必须跟着回滚，不然它会活到最后
    那次 commit —— 一条谁也解释不了的孤儿下载记录。

    ``test_a_skipped_event_does_not_stop_the_others`` 钉不住这件事：skipped 是正常返回、不抛。
    ``test_like_then_unlike...`` 钉的是 SAVEPOINT 的另一半（退出时 flush，所以同一批里的
    unlike 查得到刚插的 like）。
    """
    real_apply = external_usage._apply

    def half_write_then_blow_up(db, event, resolved_id, department, occurred_at):
        if event.event_id == "boom":
            db.add(RpAiSkillDownload(skill_id=resolved_id, eid=event.eid))
            db.flush()
            raise RuntimeError("上游数据里某个我们没想到的形状")
        return real_apply(db, event, resolved_id, department, occurred_at)

    monkeypatch.setattr(external_usage, "_apply", half_write_then_blow_up)

    r = _post(
        client,
        [
            _event(eventId="good"),
            _event(eventId="boom"),
            _event(eventId="good2"),
        ],
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert (body["applied"], body["skipped"]) == (2, 1)
    boom = next(o for o in body["outcomes"] if o["eventId"] == "boom")
    assert boom["action"] == "skipped"

    with SessionLocal() as db:
        # 恰好两条：两条好数据留下（整批回滚在对方那边看起来就是「门户挂了」），
        # 而炸掉那条半途写下的那行被回滚掉了。
        assert len(db.scalars(select(RpAiSkillDownload)).all()) == 2
        # 炸掉那条不留流水 —— 整个 SAVEPOINT 都回滚了，所以对方重推它是对的。
        ids = {e.event_id for e in db.scalars(select(RpAiExternalUsageEvent))}
        assert ids == {"good", "good2"}


def test_a_skipped_event_does_not_stop_the_others(client):
    """一个已下架的资产不能挡住同批另外 499 条。

    注意这条**不**证明回滚隔离 —— skipped 走的是正常返回，不抛异常。真正钉 SAVEPOINT 的是
    上面那条 ``test_an_event_that_raises_is_isolated...``。
    """
    r = _post(
        client,
        [
            _event(eventId="good", assetId=SKILL_UPSTREAM_ID),
            _event(eventId="bad", assetId="never-synced"),
            _event(eventId="good2", assetId=SKILL_UPSTREAM_ID),
        ],
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert (body["applied"], body["skipped"]) == (2, 1)
    with SessionLocal() as db:
        assert len(db.scalars(select(RpAiSkillDownload)).all()) == 2


# --- 形状校验 ---------------------------------------------------------------


def test_the_placeholder_verb_from_the_doc_is_refused_with_the_allowed_values(client):
    """契约样例里长期写着占位符 ``download/like``。照抄就是每条被拒。

    所以错误消息必须带上合法取值 —— 一句 unknown verb 帮不上任何忙。
    """
    r = _post(client, [_event(verb="download/like")])
    assert r.status_code == 422
    for verb in ("download", "like", "unlike"):
        assert verb in r.text


def test_an_unknown_kind_is_refused_with_the_allowed_values(client):
    """未知 ``kind`` 是 422，且消息里列出全部合法取值。

    **例子不能再用 ``tool``** —— 它从"web 型资产访问量"那一版起就是合法取值了。这里换成一个
    确实不在集合里的值；三个合法值都断言上，是为了让"以后又加了一个却忘了让消息跟着变"在这条
    测试上变红，而不是让对方照着一条不全的提示去猜。
    """
    r = _post(client, [_event(kind="dataset")])
    assert r.status_code == 422
    assert "skill" in r.text and "agent" in r.text and "tool" in r.text


def test_an_empty_batch_is_refused(client):
    assert _post(client, []).status_code == 422


def test_an_oversized_batch_is_413_not_a_gateway_timeout(client):
    events = [_event(eventId=f"rvc-{i}") for i in range(external_usage.MAX_EVENTS_PER_BATCH + 1)]
    r = _post(client, events)
    assert r.status_code == 413
    assert str(external_usage.MAX_EVENTS_PER_BATCH) in r.json()["detail"]


def test_the_eid_is_normalised(client):
    r = _post(client, [_event(eid="  ezhgmio  ")])
    assert r.status_code == 202, r.text
    with SessionLocal() as db:
        assert db.scalars(select(RpAiSkillDownload)).one().eid == EID


# --- 用户行 -----------------------------------------------------------------


def test_a_pusher_created_user_row_is_not_counted_as_having_signed_in(client):
    """这些人没登录过门户。origin 写成 ``login`` 会虚增报告里的渗透率。"""
    r = _post(client, [_event(eid="ENEWCOMER")])
    assert r.status_code == 202, r.text
    with SessionLocal() as db:
        row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == "ENEWCOMER"))
        assert row is not None
        assert row.origin != "login"
        # 部门必须一起写上，否则这个人名下的量进「无法归属」。
        assert row.department == DEPT


def test_an_existing_non_empty_department_is_not_overwritten(client):
    """登录时写下的值比目录快照新，一次推送不该把它盖回去。"""
    with SessionLocal() as db:
        db.add(RpAiPortal(eid=EID, display_name="Miao", department="RP RD3 TIC"))
        db.commit()

    assert _post(client, [_event()]).status_code == 202
    with SessionLocal() as db:
        row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == EID))
        assert row.department == "RP RD3 TIC"
        # 下载行上的部门快照跟随用户行，不是跟随目录。
        assert db.scalars(select(RpAiSkillDownload)).one().department == "RP RD3 TIC"


def test_a_directory_failure_does_not_lose_the_usage_data(client, monkeypatch):
    """部门可以下次经过时补上（只补空缺、不覆盖），用量数据丢了就是丢了。"""
    monkeypatch.setattr(external_usage, "_departments_for", lambda eids: {})
    r = _post(client, [_event()])
    assert r.status_code == 202, r.text
    assert r.json()["applied"] == 1
    with SessionLocal() as db:
        assert db.scalars(select(RpAiSkillDownload)).one().department is None
