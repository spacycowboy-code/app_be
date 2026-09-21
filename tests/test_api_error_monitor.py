"""接口报错监控：记录侧、诊断侧、接口侧。

每条关键断言都做过反面探针（把被测逻辑改错、确认恰好那条 failed、再改回），结果记在 MR 描述里。
"""
from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import database
from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.api_error import RpAiApiError
from app.services import api_errors, reporting_windows
from app.services.auth_tokens import create_token

ADMIN = "EZHGMIO"       # 在 dashboard_eids 里
OUTSIDER = "ENOTADMIN"  # 不在
SECRET = "test-secret-for-api-error-monitor"


@pytest.fixture
def client(monkeypatch):
    """鉴权打开的 client。

    全局 conftest 把 ``AUTH_ENABLED`` 关了，而这一页的全部意义就在于闸门 ——
    关着鉴权时 ``request.state.eid`` 永远是空的，测不出「名单内 vs 名单外」的区别。
    照 ``tests/test_login_stats.py`` 的现成形状来。
    """
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", SECRET)
    monkeypatch.setenv("DASHBOARD_EIDS", f"{ADMIN},ADMINMM")
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _headers(eid: str = ADMIN) -> dict:
    token, _ = create_token(eid, eid.title())
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _clean():
    """每条测试从空表开始 —— 指纹是全表唯一的，跨测试残留会让第二条测试看到别人的行。"""
    with SessionLocal() as db:
        db.query(RpAiApiError).delete()
        db.commit()
    yield


def _rows() -> list[RpAiApiError]:
    with SessionLocal() as db:
        return list(
            db.execute(
                select(RpAiApiError).order_by(RpAiApiError.id)
            ).scalars().all()
        )


# --------------------------------------------------------------------------
# 指纹
# --------------------------------------------------------------------------

def test_query_string_does_not_split_one_broken_endpoint_into_many_rows():
    """``?eid=`` 每个调用者都不同；不去掉它，一个坏接口会按人裂成很多行。"""
    a = api_errors.fingerprint("GET", "/api/market/x/download?eid=AAA", "ValueError")
    b = api_errors.fingerprint("GET", "/api/market/x/download?eid=BBB", "ValueError")
    assert a == b


def test_different_ids_are_deliberately_different_rows():
    """同一个端点、不同的数据行，大概率是不同的根因 —— 刻意不做 id 归一化。"""
    a = api_errors.fingerprint("GET", "/api/skills/foo", "ValidationError")
    b = api_errors.fingerprint("GET", "/api/skills/bar", "ValidationError")
    assert a != b


def test_method_and_exception_type_are_part_of_the_identity():
    base = api_errors.fingerprint("GET", "/api/skills/foo", "ValidationError")
    assert api_errors.fingerprint("POST", "/api/skills/foo", "ValidationError") != base
    assert api_errors.fingerprint("GET", "/api/skills/foo", "KeyError") != base


# --------------------------------------------------------------------------
# 记录侧
# --------------------------------------------------------------------------

def test_a_crash_is_recorded_with_the_app_frames_only(client, monkeypatch):
    """抛异常的 5xx 要落一行，且调用栈里不该塞满框架帧。"""
    from app.routers import api_errors as router_mod

    def boom(*_a, **_k):
        raise RuntimeError("探针：故意炸")

    monkeypatch.setattr(router_mod, "_get_or_404", boom)
    with pytest.raises(RuntimeError):
        client.post("/api/admin/api-errors/1/resolve", headers=_headers())

    rows = _rows()
    assert len(rows) == 1
    assert rows[0].status == 500
    assert rows[0].exc_type == "RuntimeError"
    assert "探针：故意炸" in (rows[0].exc_message or "")
    assert "site-packages" not in (rows[0].traceback or "")


def test_the_same_failure_twice_bumps_the_count_instead_of_adding_a_row():
    with SessionLocal() as db:
        for _ in range(3):
            api_errors.record(
                db, method="GET", path="/api/x", status=500,
                exc=ValueError("同一件事"),
            )
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].count == 3
    assert rows[0].first_seen <= rows[0].last_seen


def test_an_explicit_5xx_with_no_exception_is_still_recorded():
    """「服务在，它依赖的东西不在」这一类没有异常对象，但恰恰最常被用户问起。"""
    with SessionLocal() as db:
        api_errors.record(db, method="GET", path="/api/y", status=503, exc=None)
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].status == 503
    assert rows[0].exc_type is None
    assert rows[0].traceback is None


def test_a_4xx_is_not_recorded(client):
    """404 不是故障。图片 404 一天上百次，混进来会把真信号淹掉。"""
    resp = client.post("/api/admin/api-errors/999999/resolve", headers=_headers())
    assert resp.status_code == 404
    assert _rows() == []


def test_a_403_from_the_gate_is_not_recorded_either(client):
    """名单外的人被挡下不是故障，是闸门在干活。"""
    resp = client.get("/api/admin/api-errors", headers=_headers(OUTSIDER))
    assert resp.status_code == 403
    assert _rows() == []


def test_recording_failure_never_changes_the_response(client, monkeypatch):
    """记录自己坏了，用户看到的东西不许因此改变。"""
    def broken_fingerprint(*_a, **_k):
        raise RuntimeError("探针：记录侧自己炸了")

    monkeypatch.setattr(api_errors, "fingerprint", broken_fingerprint)
    with SessionLocal() as db:
        assert api_errors.record(
            db, method="GET", path="/api/z", status=500, exc=ValueError("x")
        ) is None
    assert _rows() == []


def test_a_recurrence_reopens_a_resolved_row():
    """标记过「已处理」的问题又回来了，必须重新看见。"""
    with SessionLocal() as db:
        row = api_errors.record(
            db, method="GET", path="/api/again", status=500, exc=ValueError("v")
        )
        row.resolved_at = row.last_seen
        row.resolved_by = "EZHGMIO"
        db.commit()
        api_errors.record(
            db, method="GET", path="/api/again", status=500, exc=ValueError("v")
        )
    rows = _rows()
    assert rows[0].resolved_at is None
    assert rows[0].resolved_by is None
    assert rows[0].count == 2


# --------------------------------------------------------------------------
# 判定器回复的三种形态
# --------------------------------------------------------------------------

FOUR = '{"user": "u", "cause": "c", "scope": "s", "action": "a"}'


@pytest.mark.parametrize(
    "raw, label",
    [
        (FOUR, "纯 JSON"),
        (json.dumps({"response": FOUR}), "包一层 response"),
        ("Let me check the code first.\n" + FOUR + "\n希望有帮助。", "前后带散文"),
    ],
)
def test_all_three_shapes_of_judge_reply_parse(monkeypatch, raw, label):
    """判定器是带工具的审计 agent：会自说自话，也会被 ACP 层要求包成 JSON。

    三种都要认 —— 少认一种就会把一次成功的诊断记成失败。
    """
    monkeypatch.setattr(api_errors, "ask_judge", lambda _p: raw)
    with SessionLocal() as db:
        row = api_errors.record(
            db, method="GET", path=f"/api/shape/{label}", status=500,
            exc=ValueError("v"),
        )
        api_errors.diagnose(db, row)
        assert row.diagnosis_user == "u", label
        assert row.diagnosis_cause == "c"
        assert row.diagnosis_scope == "s"
        assert row.diagnosis_action == "a"
        assert row.diagnosed_at is not None
        assert row.diagnosis_error is None


def test_llm_failure_records_why_and_leaves_it_pending(monkeypatch):
    """失败要写原因（否则页面一片空白，读起来像还没轮到它），且下一轮要能重试。"""
    def unreachable(_p):
        raise urllib.error.URLError("探针：判定器不可达")

    monkeypatch.setattr(api_errors, "ask_judge", unreachable)
    with SessionLocal() as db:
        row = api_errors.record(
            db, method="GET", path="/api/nollm", status=500, exc=ValueError("v")
        )
        api_errors.diagnose(db, row)
        assert row.diagnosed_at is None          # 仍在待诊断队列里
        assert "探针：判定器不可达" in (row.diagnosis_error or "")
        assert api_errors.diagnose_pending(db, limit=5)["examined"] == 1


def test_unparseable_reply_is_a_failure_not_a_blank_diagnosis(monkeypatch):
    monkeypatch.setattr(api_errors, "ask_judge", lambda _p: "我觉得应该没事吧")
    with SessionLocal() as db:
        row = api_errors.record(
            db, method="GET", path="/api/prose", status=500, exc=ValueError("v")
        )
        api_errors.diagnose(db, row)
        assert row.diagnosed_at is None
        assert "解析不出 JSON" in (row.diagnosis_error or "")


def test_diagnose_pending_respects_the_batch_limit(monkeypatch):
    """一轮不许无限做：这跑在 reconciler 循环里，会连带把技能审计的收敛也停掉。"""
    monkeypatch.setattr(api_errors, "ask_judge", lambda _p: FOUR)
    with SessionLocal() as db:
        for i in range(5):
            api_errors.record(
                db, method="GET", path=f"/api/batch/{i}", status=500,
                exc=ValueError("v"),
            )
        assert api_errors.diagnose_pending(db, limit=2)["diagnosed"] == 2
        remaining = db.execute(
            select(RpAiApiError).where(RpAiApiError.diagnosed_at.is_(None))
        ).scalars().all()
        assert len(remaining) == 3


def test_a_flood_falls_back_to_one_per_sweep(monkeypatch):
    """涌入时只做 1 条。

    判定器是与技能审计共用的服务（日均 8.5 次模型调用），而涌入通常是同一个故障按 id 裂成
    很多指纹 —— 给第 50 个变体出诊断没有新信息，却会占住判定器。
    """
    calls = []
    monkeypatch.setattr(
        api_errors, "ask_judge", lambda _p: calls.append(1) or FOUR
    )
    with SessionLocal() as db:
        for i in range(12):
            api_errors.record(
                db, method="GET", path=f"/api/flood/{i}", status=500,
                exc=ValueError("v"),
            )
        result = api_errors.diagnose_pending(db, limit=5, flood_threshold=10)
        assert result["flooding"] is True
        assert result["pending"] == 12
        # limit 是 5，但涌入时只做 1 条 —— 判定器只被调了一次。
        assert result["diagnosed"] == 1
        assert len(calls) == 1


def test_below_the_threshold_the_batch_limit_still_applies(monkeypatch):
    """阈值以下不该被防洪影响，否则批量上限就永远等于 1 了。"""
    monkeypatch.setattr(api_errors, "ask_judge", lambda _p: FOUR)
    with SessionLocal() as db:
        for i in range(4):
            api_errors.record(
                db, method="GET", path=f"/api/calm/{i}", status=500,
                exc=ValueError("v"),
            )
        result = api_errors.diagnose_pending(db, limit=3, flood_threshold=10)
        assert result["flooding"] is False
        assert result["diagnosed"] == 3


def test_the_shipped_default_batch_is_one():
    """稳态每天不到一条新指纹，默认 1 就够；有区别的只有涌入那天，而那天不该抢判定器。"""
    assert get_settings().error_diagnosis_batch == 1


# --------------------------------------------------------------------------
# 接口侧
# --------------------------------------------------------------------------

def test_only_dashboard_eids_may_read_the_page(client):
    """前端侧边栏隐藏不是安全边界；这一页会吐调用栈和工号，闸门必须在服务端。

    三个端点各打一次 —— 只测列表会漏掉写操作，而写操作才是能改状态的那两个。
    """
    assert client.get(
        "/api/admin/api-errors", headers=_headers(OUTSIDER)
    ).status_code == 403
    assert client.post(
        "/api/admin/api-errors/1/diagnose", headers=_headers(OUTSIDER)
    ).status_code == 403
    assert client.post(
        "/api/admin/api-errors/1/resolve", headers=_headers(OUTSIDER)
    ).status_code == 403
    # 无凭据同样进不来（站点级闸门先挡）。
    assert client.get("/api/admin/api-errors").status_code == 401
    # 名单内的人拿得到数据。
    assert client.get("/api/admin/api-errors", headers=_headers()).status_code == 200


def test_the_gate_helper_names_what_was_refused():
    """403 的措辞里要有动作名 —— 共享的「无权限」读不出缺的是哪一个管理面。"""
    from app.services import eids as eids_mod

    class _Req:
        class state:
            eid = OUTSIDER

    with pytest.raises(HTTPException) as caught:
        eids_mod.require_dashboard_eid(_Req(), "查看接口报错")
    assert caught.value.status_code == 403
    assert "查看接口报错" in caught.value.detail


def test_the_list_hides_resolved_rows_by_default_and_total_matches_the_filter(client):
    with SessionLocal() as db:
        keep = api_errors.record(
            db, method="GET", path="/api/keep", status=500, exc=ValueError("v")
        )
        gone = api_errors.record(
            db, method="GET", path="/api/gone", status=500, exc=KeyError("k")
        )
        gone.resolved_at = gone.last_seen
        gone.resolved_by = "EZHGMIO"
        db.commit()
        keep_path = keep.path

    body = client.get("/api/admin/api-errors", headers=_headers()).json()
    assert [i["path"] for i in body["items"]] == [keep_path]
    # total 必须跟着同一个过滤条件走，否则页面上「共 N 条」会让人以为漏了记录。
    assert body["total"] == 1

    all_body = client.get(
        "/api/admin/api-errors?includeResolved=true", headers=_headers()
    ).json()
    assert all_body["total"] == 2


def test_diagnosis_is_null_until_there_is_one(client, monkeypatch):
    """空对象和「有结论但内容为空」在页面上分不开，所以没结论时必须是 null。"""
    with SessionLocal() as db:
        api_errors.record(
            db, method="GET", path="/api/pending", status=500, exc=ValueError("v")
        )
    item = client.get("/api/admin/api-errors", headers=_headers()).json()["items"][0]
    assert item["diagnosis"] is None

    monkeypatch.setattr(api_errors, "ask_judge", lambda _p: FOUR)
    resp = client.post(
        f"/api/admin/api-errors/{item['id']}/diagnose", headers=_headers()
    )
    assert resp.status_code == 200
    assert resp.json()["diagnosis"] == {
        "user": "u", "cause": "c", "scope": "s", "action": "a"
    }


def test_resolve_is_idempotent_and_records_who(client):
    with SessionLocal() as db:
        row = api_errors.record(
            db, method="GET", path="/api/res", status=500, exc=ValueError("v")
        )
        row_id = row.id

    first = client.post(
        f"/api/admin/api-errors/{row_id}/resolve", headers=_headers()
    ).json()
    assert first["resolvedAt"] is not None
    assert first["resolvedBy"]
    second = client.post(
        f"/api/admin/api-errors/{row_id}/resolve", headers=_headers()
    ).json()
    assert second["resolvedAt"] == first["resolvedAt"]


def test_timestamps_go_out_labelled(client):
    """裸时间戳会被浏览器按自己的时区读，静默偏掉库与浏览器之间的时差。"""
    with SessionLocal() as db:
        api_errors.record(
            db, method="GET", path="/api/tz", status=500, exc=ValueError("v")
        )
    item = client.get("/api/admin/api-errors", headers=_headers()).json()["items"][0]
    assert item["lastSeen"].endswith("+00:00") or item["lastSeen"].endswith("Z")


def test_the_emitted_instant_is_right_when_the_db_clock_is_not_utc(client, monkeypatch):
    """库时钟不等于 UTC 时，发出去的**绝对时刻**仍必须是真实时刻。

    上面那条只看结尾有没有 ``+00:00`` —— 而口径错了 2 小时的时间戳照样带 ``+00:00``，
    生产上就是这么错过去的：写入用的是容器时钟（UTC），序列化按全仓约定「naive = 库本地
    时间」给它盖上 ``db_utc_offset()``（这台库是 UTC+2），于是绝对时刻早了 2 小时，页面上
    显示 09:03 而真实是 11:03，不抛任何异常。

    必须伪装库偏移才测得出来：hermetic 套件跑 sqlite，``TIMESTAMPDIFF`` 不存在、
    ``db_utc_offset()`` 落到返回 0 的兜底分支，而偏移为 0 时写错口径与写对口径的输出
    **完全一样**。
    """
    offset = timedelta(hours=2)
    monkeypatch.setattr(database, "db_utc_offset", lambda: offset)
    monkeypatch.setattr(reporting_windows, "db_utc_offset", lambda: offset)

    before = datetime.now(timezone.utc)
    with SessionLocal() as db:
        api_errors.record(
            db, method="GET", path="/api/tz-abs", status=500, exc=ValueError("v")
        )
    after = datetime.now(timezone.utc)

    items = client.get("/api/admin/api-errors", headers=_headers()).json()["items"]
    item = next(i for i in items if i["path"] == "/api/tz-abs")

    for column in ("firstSeen", "lastSeen"):
        emitted = datetime.fromisoformat(item[column])
        assert emitted.tzinfo is not None, f"{column} 没带时区"
        slack = timedelta(seconds=5)
        assert before - slack <= emitted <= after + slack, (
            f"{column} 偏了：发出 {emitted}，真实区间 {before}–{after}"
        )
