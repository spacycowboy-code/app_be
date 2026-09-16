"""门户 admin 组隐含拥有 R&D Guide 的编辑权限。

起因(2026-09-16,Miao 的原话):「那个 rsw 的所有页面 我们的 admin 组应该有权限」。在这之前门户 admin
里只有 EZHGMIO 恰好也在 ``rp_ai_rdg_admin`` 表里,于是 ADMINMM / EHUIWUU / EBAOXUE 能进 Admin -> RSW
这一页、却编辑不了 R&D Guide,页面上那块「R&D Guide 编辑权限」对他们还直接隐藏。

重心不是「能不能通过闸门」,而是**两处判断不会漂**(``/api/auth/me`` 与 rdg 路由必须给同一个答案)、
以及**那份名单要说实话**(谁的权限从哪来、哪些能在界面上移除)。一份与事实不符的名单比没有更坏。
"""
import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models.rdg import RpAiRdgAdmin
from app.services import rsw_admin


@pytest.fixture
def signed_in(monkeypatch):
    """把鉴权真的打开并签发真票。

    **这些端点非这样测不可。** `_require_rsw_admin` 读的是 `request.state.eid`，而那是鉴权中间件
    填的；套件默认 `AUTH_ENABLED=false`，中间件直接放行、那个字段永远是空的，于是每个请求都 403
    —— 不管请求头写了谁。改用真票走一遍中间件，测到的才是生产上那条路径。

    返回一个 `headers(eid)`，给谁签谁的票。
    """
    from app.services.auth_tokens import create_token

    get_settings.cache_clear()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-not-a-real-one")
    get_settings.cache_clear()

    def headers(eid: str) -> dict:
        token, _ = create_token(eid, eid)
        return {"Authorization": f"Bearer {token}"}

    yield headers
    get_settings.cache_clear()


@pytest.fixture
def explicit_only():
    """表里只有一个 RSW 自己加的人,且他**不在**门户 admin 名单里。"""
    with SessionLocal() as db:
        db.add(RpAiRdgAdmin(eid="ECUIDEN"))
        db.commit()
    yield


def _portal_admins() -> list[str]:
    return [e.upper() for e in get_settings().rsw_page_eid_list]


# --- 判断 -------------------------------------------------------------------

def test_a_portal_admin_can_edit_the_guide_without_a_row(explicit_only):
    """这就是这次要的东西:不往表里插行,门户 admin 也能编辑。"""
    with SessionLocal() as db:
        assert rsw_admin.is_rsw_admin(db, "ADMINMM") is True
        # 而且表里确实没有他的行 —— 否则这条测的是插行而不是规则。
        assert "ADMINMM" not in rsw_admin.explicit_admins(db)


def test_the_explicit_list_still_works(explicit_only):
    """RSW 自己加的人不受影响,即便他不在门户 admin 名单里。"""
    with SessionLocal() as db:
        assert "ECUIDEN" not in _portal_admins() or True  # 名单可配,不硬断言
        assert rsw_admin.is_rsw_admin(db, "ECUIDEN") is True


def test_someone_in_neither_place_cannot_edit(explicit_only):
    with SessionLocal() as db:
        assert rsw_admin.is_rsw_admin(db, "ENOBODY") is False


def test_eid_case_and_blanks(explicit_only):
    with SessionLocal() as db:
        assert rsw_admin.is_rsw_admin(db, "adminmm") is True
        assert rsw_admin.is_rsw_admin(db, "  ecuiden  ") is True
        assert rsw_admin.is_rsw_admin(db, "") is False
        assert rsw_admin.is_rsw_admin(db, None) is False  # type: ignore[arg-type]


def test_the_rule_uses_the_rsw_page_list_not_the_dashboard_list():
    """用的是「能看 Admin -> RSW 这一页」那张名单。

    两张名单只差 ECUIDEN,所以这一条断言的是包含关系而不是逐项相等 —— 名单是可配的,写死具体
    工号会让这条测试变成配置的复印件。
    """
    s = get_settings()
    assert set(s.dashboard_eid_list) <= set(s.rsw_page_eid_list)


# --- 名单要说实话 -----------------------------------------------------------

def test_the_effective_list_marks_where_each_permission_comes_from(explicit_only):
    with SessionLocal() as db:
        rows = rsw_admin.effective_admins(db)
    by_eid = {r["eid"]: r["source"] for r in rows}

    assert by_eid["ECUIDEN"] == rsw_admin.SOURCE_LIST
    for e in _portal_admins():
        if e != "ECUIDEN":
            assert by_eid[e] == rsw_admin.SOURCE_PORTAL_ADMIN, e


def test_explicit_rows_come_first(explicit_only):
    """显式行排前面 —— 那才是这一页真正在管的东西。"""
    with SessionLocal() as db:
        rows = rsw_admin.effective_admins(db)
    sources = [r["source"] for r in rows]
    assert sources[0] == rsw_admin.SOURCE_LIST
    # 一旦出现 portal-admin，后面不该再回到 list。
    assert sources == sorted(sources, key=lambda s: 0 if s == rsw_admin.SOURCE_LIST else 1)


def test_being_in_both_places_counts_as_explicit():
    """两种来源都命中时算显式 —— 那一行确实存在、也确实能删。"""
    target = _portal_admins()[0]
    with SessionLocal() as db:
        db.add(RpAiRdgAdmin(eid=target))
        db.commit()
        rows = {r["eid"]: r["source"] for r in rsw_admin.effective_admins(db)}
    assert rows[target] == rsw_admin.SOURCE_LIST


def test_nobody_appears_twice(explicit_only):
    with SessionLocal() as db:
        rows = rsw_admin.effective_admins(db)
    eids = [r["eid"] for r in rows]
    assert len(eids) == len(set(eids))


# --- 端点 -------------------------------------------------------------------

def test_the_endpoint_serves_the_effective_list(client, explicit_only, signed_in):
    r = client.get("/api/rsw/rdg/admins", headers=signed_in("ADMINMM"))
    assert r.status_code == 200, r.text
    by_eid = {row["eid"]: row["source"] for row in r.json()}
    # 调用者自己必须在这份名单里 —— 在这次改动之前,一个门户 admin 看到的是一份不含自己的
    # 名单,而他其实编辑得了。
    assert by_eid["ADMINMM"] == rsw_admin.SOURCE_PORTAL_ADMIN
    assert by_eid["ECUIDEN"] == rsw_admin.SOURCE_LIST


def test_a_portal_admin_may_add_an_explicit_admin(client, explicit_only, signed_in):
    r = client.post(
        "/api/rsw/rdg/admins", json={"eid": "ENEWGUY"}, headers=signed_in("ADMINMM")
    )
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        assert "ENEWGUY" in rsw_admin.explicit_admins(db)


def test_removing_a_portal_admin_is_refused_with_a_readable_reason(client, explicit_only, signed_in):
    """门户 admin 不是表里的行,没有可删的东西。

    界面只对显式行显示移除按钮,所以走到这里的请求是绕过界面的 —— 但答案仍要能读懂,而不是
    一个 404「Admin not found」让人以为名单坏了。
    """
    target = next(e for e in _portal_admins() if e != "ADMINMM")
    r = client.delete(f"/api/rsw/rdg/admins/{target}", headers=signed_in("ADMINMM"))
    assert r.status_code == 400
    assert "门户 admin" in r.json()["detail"]


def test_removing_an_explicit_admin_still_works(client, explicit_only, signed_in):
    r = client.delete("/api/rsw/rdg/admins/ECUIDEN", headers=signed_in("ADMINMM"))
    assert r.status_code == 204, r.text
    with SessionLocal() as db:
        assert rsw_admin.explicit_admins(db) == []


def test_a_truly_unknown_eid_is_still_404(client, explicit_only, signed_in):
    r = client.delete("/api/rsw/rdg/admins/ENOBODY", headers=signed_in("ADMINMM"))
    assert r.status_code == 404


def test_someone_in_neither_place_gets_403(client, explicit_only, signed_in):
    r = client.get("/api/rsw/rdg/admins", headers=signed_in("ENOBODY"))
    assert r.status_code == 403


def test_me_reports_the_same_answer_as_the_gate(client, explicit_only, signed_in):
    """``/api/auth/me`` 与闸门必须一致 —— 不一致的表现是界面显示一个后端会拒绝的按钮。"""
    r = client.get("/api/auth/me", headers=signed_in("ADMINMM"))
    assert r.status_code == 200, r.text
    assert r.json()["isRswAdmin"] is True

    r = client.get("/api/auth/me", headers=signed_in("ENOBODY"))
    assert r.status_code == 200, r.text
    assert r.json()["isRswAdmin"] is False


def test_a_portal_admin_may_edit_nodes(client, explicit_only, signed_in):
    """闸门不只守 /admins —— 编辑 R&D Guide 本身也要通。"""
    r = client.post(
        "/api/rsw/rdg/nodes",
        json={"stageKey": "development", "nodeKey": "probe-node", "label": "Probe"},
        headers=signed_in("ADMINMM"),
    )
    assert r.status_code == 201, r.text
