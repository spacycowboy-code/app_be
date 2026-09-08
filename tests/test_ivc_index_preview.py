"""贴进来的 IVC 索引：谁能贴，以及贴进来之后看到什么。

数据源是 IVC 的 Nexus 索引。门户自己抓不到它 —— 实测生产宿主机到那台机器 ICMP 通而 TCP
22/80/443/8081/8443 全部静默超时、且无代理配置，所以入口做成人工粘贴。样本存在 workspace 的
``ivc-nexus-index-sample.json``（17 个 skill + 1 个 agent）。

这里钉三组东西：**权限**（服务端判定，不靠前端藏入口）、**体检**（从真实样本里看出来的四个坑）、
以及**这个端点不写库**这件事本身。最后一条值得一条测试，因为"预览"和"同步"做在同一个按钮后面
时，很容易在后来的改动里悄悄变成真的写库。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.market import RpAiMarketItem
from app.models.skill import RpAiSkill
from app.services import ivc_index
from app.services.auth_tokens import create_token

ALLOWED = "EZHGMIO"          # 在 dashboard_eids 里
ALLOWED_ADMIN = "ADMINMM"    # 同上
OUTSIDER = "EOTHER1"         # 不在

SAMPLE = (
    Path("/workspace/home/ezhgmio/.kiro/crew/workspace")
    / "ivc-nexus-index-sample.json"
)
NEXUS = "https://arm1s10-eiffel239.eiffel.gic.ericsson.se:8443/nexus/content/repositories"


@pytest.fixture()
def client(monkeypatch):
    """鉴权打开：``request.state.eid`` 是权限判定的唯一依据。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "ivc-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    return TestClient(app)


def _auth(eid: str) -> dict:
    token, _ = create_token(eid)
    return {"Authorization": f"Bearer {token}"}


def _skill(**over) -> dict:
    raw = {
        "author": "euhazhi",
        "created_at": "1788170082",
        "description": "Process CATT OTA test result folders.",
        "display_name": "Huishuo Zhao",
        "download_url": f"{NEXUS}/releases/skills/catt/0.0.1/catt-0.0.1.tar.gz",
        "id": "catt-ota-result-calculator",
        "name": "catt-ota-result-calculator",
        "skill_md_url": f"{NEXUS}/releases/skills/catt/0.0.1/SKILL.md",
        "tags": [],
        "version": "0.0.1",
    }
    raw.update(over)
    return raw


def _agent(**over) -> dict:
    raw = {
        "created_at": "1788170082",
        "deprecated": False,
        "description": "客户站问题复现助手.",
        "id": "repro-expert",
        "name": "repro-expert",
        "owner": "wenjie.a.liu",
        "runtimes": [
            {
                "runtime": "kiro",
                "download_url": f"{NEXUS}/releases/agents/repro-expert/1.0.0/kiro/x.tar.gz",
            }
        ],
        "tags": ["radio"],
        "version": "1.0.0",
        "visibility": "internal",
    }
    raw.update(over)
    return raw


# --- 权限 -------------------------------------------------------------------


def test_an_allowed_eid_can_preview(client):
    r = client.post(
        "/api/integrations/ivc/preview",
        json={"skills": [_skill()], "agents": []},
        headers=_auth(ALLOWED),
    )
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1


def test_the_admin_account_can_too(client):
    r = client.post(
        "/api/integrations/ivc/preview",
        json={"skills": [_skill()], "agents": []},
        headers=_auth(ALLOWED_ADMIN),
    )
    assert r.status_code == 200, r.text


def test_everyone_else_is_refused_by_the_server(client):
    """前端把入口藏起来不是安全边界 —— 这个端点公开可达，绕过界面一样能到。"""
    r = client.post(
        "/api/integrations/ivc/preview",
        json={"skills": [_skill()], "agents": []},
        headers=_auth(OUTSIDER),
    )
    assert r.status_code == 403


def test_anonymous_is_refused(client):
    r = client.post(
        "/api/integrations/ivc/preview", json={"skills": [_skill()], "agents": []}
    )
    assert r.status_code in (401, 403)


# --- 这个端点不写库 ---------------------------------------------------------


def test_preview_writes_nothing(client):
    """索引里的 download_url 指向不可达的 Nexus，所以包体拿不到、也就无法入库。

    如果哪天这个端点开始真的建条目，这条会失败 —— 那正是需要被看见的时刻。
    """
    with SessionLocal() as db:
        before = (db.query(RpAiSkill).count(), db.query(RpAiMarketItem).count())

    client.post(
        "/api/integrations/ivc/preview",
        json={"skills": [_skill()], "agents": [_agent()]},
        headers=_auth(ALLOWED),
    )

    with SessionLocal() as db:
        after = (db.query(RpAiSkill).count(), db.query(RpAiMarketItem).count())
    assert before == after


# --- 体检：四个从真实样本里看出来的坑 ---------------------------------------


def test_the_shared_created_at_is_reported_as_index_time(client):
    """18 条全等，说明它是索引生成时间。当成创建日期用会让「本月新增」一次虚增 18。"""
    r = client.post(
        "/api/integrations/ivc/preview",
        json={"skills": [_skill(), _skill(id="b", name="b")], "agents": []},
        headers=_auth(ALLOWED),
    ).json()

    assert r["generatedAt"] is not None
    assert any("索引生成时间" in n for n in r["notes"])


def test_lowercase_authors_are_normalised_to_uppercase(client):
    """样本里 16 个小写、EPENYAN 大写。不归一化，同一个人会被算成两个。"""
    r = client.post(
        "/api/integrations/ivc/preview",
        json={"skills": [_skill(author="euhazhi"), _skill(id="b", name="b", author="EPENYAN")],
              "agents": []},
        headers=_auth(ALLOWED),
    ).json()

    assert [e["eid"] for e in r["entries"]] == ["EUHAZHI", "EPENYAN"]


def test_an_agent_owner_without_a_mapping_is_flagged_not_guessed(client):
    """``owner`` 是 ``wenjie.a.liu``，报告只认工号。

    猜一个工号会把资产记到别人部门下；留空并说明，是这里唯一诚实的做法。
    """
    r = client.post(
        "/api/integrations/ivc/preview",
        json={"skills": [], "agents": [_agent()]},
        headers=_auth(ALLOWED),
    ).json()

    entry = r["entries"][0]
    assert entry["rawOwner"] == "wenjie.a.liu"
    assert entry["eid"] is None
    assert any("还没有对应的工号" in p for p in entry["problems"])


def test_an_agent_owner_that_is_already_an_eid_needs_no_mapping(client):
    """``owner`` 有时本来就是工号（``EIWEULN``），不该因为映射表里没有它而被挡下。

    映射表存在是因为 ``wenjie.a.liu`` 这种用户名形态推不出工号。已经是工号的走同一张表，
    意味着要为每个人补一行键与值完全相同的映射 —— 漏一行，资产就归不到部门。
    """
    r = client.post(
        "/api/integrations/ivc/preview",
        json={"skills": [], "agents": [_agent(owner="EIWEULN")]},
        headers=_auth(ALLOWED),
    ).json()

    entry = r["entries"][0]
    assert entry["rawOwner"] == "EIWEULN"
    assert entry["eid"] == "EIWEULN"
    assert entry["problems"] == []
    assert r["usable"] == 1


def test_a_lowercase_owner_in_eid_form_is_normalised(client):
    """工号归一化成大写，否则同一个人会被算两次 —— 和 skill 的 ``author`` 同一套规则。"""
    r = client.post(
        "/api/integrations/ivc/preview",
        json={"skills": [], "agents": [_agent(owner="eiweuln")]},
        headers=_auth(ALLOWED),
    ).json()

    entry = r["entries"][0]
    assert entry["rawOwner"] == "eiweuln"
    assert entry["eid"] == "EIWEULN"
    assert entry["problems"] == []


def test_a_download_url_off_the_known_host_is_rejected(client):
    """URL 来自一次人工粘贴。一个能被换成任意地址的下载按钮，比少同步一个资产危险得多。"""
    r = client.post(
        "/api/integrations/ivc/preview",
        json={"skills": [_skill(download_url="https://evil.example/x.tar.gz")], "agents": []},
        headers=_auth(ALLOWED),
    ).json()

    assert r["usable"] == 0 and r["rejected"] == 1
    assert any("不在允许的主机上" in p for p in r["entries"][0]["problems"])


def test_one_bad_entry_does_not_reject_the_rest(client):
    r = client.post(
        "/api/integrations/ivc/preview",
        json={
            "skills": [_skill(), _skill(id="b", name="b", version="")],
            "agents": [],
        },
        headers=_auth(ALLOWED),
    ).json()

    assert r["total"] == 2 and r["usable"] == 1 and r["rejected"] == 1


# --- 整份索引形状不对 -------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        [],                                   # 最外层是数组
        {"nothing": 1},                       # 两个键都没有
        {"skills": {"not": "a list"}},        # skills 不是数组
        {"skills": [], "agents": []},         # 一条资产都没有
    ],
)
def test_a_malformed_index_is_a_400_with_a_reason(client, payload):
    r = client.post(
        "/api/integrations/ivc/preview", json=payload, headers=_auth(ALLOWED)
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]


# --- 真实样本 ---------------------------------------------------------------


def test_the_real_sample_parses_with_only_the_agent_flagged():
    """17 个 skill 全部可用；唯一被挡下的是那个 owner 映射不出来的 agent。

    直接调解析层而不走 HTTP：断言的是数据源与解析规则的吻合，与鉴权无关。
    """
    if not SAMPLE.exists():
        pytest.skip("index sample not present")
    parsed = ivc_index.parse(json.loads(SAMPLE.read_text(encoding="utf-8")))

    assert len(parsed.entries) == 18
    assert len(parsed.usable) == 17
    assert [e.id for e in parsed.rejected] == ["repro-expert"]
    # 8 位 skill 作者，全部归一化成大写工号。
    eids = {e.eid for e in parsed.entries if e.kind == "skill"}
    assert len(eids) == 8
    assert all(e and e.isupper() and e.startswith("E") for e in eids)


# --- 真正入库 ---------------------------------------------------------------


@pytest.fixture()
def ivc_category():
    """同步落在既有的 ``ivc`` 分组下。生产库里它已经存在，测试库只种了 ``public``。"""
    from app.models.category import RpAiCategory

    with SessionLocal() as db:
        if db.get(RpAiCategory, "ivc") is None:
            db.add(RpAiCategory(id="ivc", label="IVC"))
            db.commit()
    yield "ivc"
    with SessionLocal() as db:
        db.query(RpAiSkill).filter(RpAiSkill.id.like("ivc-%")).delete(
            synchronize_session=False
        )
        db.commit()


def _sync(client, body, eid=ALLOWED):
    return client.post("/api/integrations/ivc/sync", json=body, headers=_auth(eid))


def test_sync_registers_the_skill_without_claiming_to_store_it(client, ivc_category):
    r = _sync(client, {"skills": [_skill()], "agents": []})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1

    with SessionLocal() as db:
        skill = db.get(RpAiSkill, "ivc-catt-ota-result-calculator")
        assert skill is not None
        assert skill.status == "approved"
        assert skill.external_source == "nexus-ivc"
        assert skill.external_url.startswith("https://arm1s10-eiffel239")
        # 内容不在我们的仓库里，这两个必须留空 —— 填了会让下载路径去读不存在的字节。
        assert skill.storage_path is None and skill.repo_commit is None
        assert skill.externally_hosted is True


def test_the_id_is_prefixed_so_a_name_clash_cannot_collide(client, ivc_category):
    """IVC 有 ``skill-creator``，门户已有 ``anthropics-skill-creator``（name 也叫它）。

    口径是操作者定的：重名没关系，上传人不一样就行。所以名字照原样留着，只让 id 不撞。
    """
    _sync(client, {"skills": [_skill(id="skill-creator", name="skill-creator")], "agents": []})
    with SessionLocal() as db:
        assert db.get(RpAiSkill, "ivc-skill-creator") is not None
        assert db.get(RpAiSkill, "skill-creator") is None  # 没有占用上游那个裸 id


def test_an_author_who_never_signed_in_gets_a_user_row(client, ivc_category):
    """``uploader_eid`` 是外键，而 8 位作者里有 5 个从未登录门户。

    不补这一行同步会因外键失败；补一个空行则卡片上只能显示工号，而上游给了人名。
    """
    from app.models.user import RpAiPortal

    _sync(client, {"skills": [_skill(author="enewguy", display_name="New Guy")], "agents": []})
    with SessionLocal() as db:
        row = db.query(RpAiPortal).filter(RpAiPortal.eid == "ENEWGUY").first()
        assert row is not None and row.display_name == "New Guy"
        skill = db.get(RpAiSkill, "ivc-catt-ota-result-calculator")
        assert skill.author == "ENEWGUY"        # 存工号，与门户其余技能一致
        assert skill.creator_label == "New Guy"  # 卡片上显示人名


def test_an_imported_author_is_not_counted_as_having_signed_in(client, ivc_category):
    """导入建的账号行必须标 ``origin='import'``。

    ``rp_ai_portal`` 的行在这一列加上来之前全都是首次登录时建的，所以报告把「表里有一行」
    读成「这个人用过门户」（figures.py 的 ever_signed_in）。同步在这里打破了那个等价：
    这些人从未登录，行只是为了把他们的技能挂到一个部门上。默认值是 ``login``，所以不显式
    写就会让「曾登录过的人数」凭空多出 IVC 那 5 个从未登录的作者。
    """
    from app.models.user import RpAiPortal

    _sync(client, {"skills": [_skill(author="enewguy", display_name="New Guy")], "agents": []})
    with SessionLocal() as db:
        assert db.query(RpAiPortal).filter(RpAiPortal.eid == "ENEWGUY").first().origin == "import"


def test_syncing_does_not_demote_someone_who_really_signed_in(client, ivc_category):
    """已经登录过的人,导入不该把他的行降级成 import ——那会把他从渗透率里除名。"""
    from app.models.user import RpAiPortal

    with SessionLocal() as db:
        db.add(RpAiPortal(eid="EPENYAN", display_name="Kelvin Yang", origin="login"))
        db.commit()

    _sync(client, {"skills": [_skill(author="epenyan", display_name="Kelvin Yang")], "agents": []})
    with SessionLocal() as db:
        assert db.query(RpAiPortal).filter(RpAiPortal.eid == "EPENYAN").first().origin == "login"


def test_syncing_twice_updates_rather_than_duplicates(client, ivc_category):
    _sync(client, {"skills": [_skill()], "agents": []})
    again = _sync(client, {"skills": [_skill()], "agents": []}).json()
    assert again["created"] == 0 and again["unchanged"] == 1

    bumped = _sync(client, {"skills": [_skill(version="0.0.2")], "agents": []}).json()
    assert bumped["updated"] == 1
    assert "0.0.1 -> 0.0.2" in bumped["outcomes"][0]["detail"]
    with SessionLocal() as db:
        assert db.get(RpAiSkill, "ivc-catt-ota-result-calculator").version == "0.0.2"


def test_the_agent_is_skipped_with_the_reason_stated(client, ivc_category):
    """agent 走包上传管线，那条路要包体，而包体在不可达的 Nexus 上。

    明确跳过并说明，而不是勉强把它登记成一个技能 —— 后者会让市场里出现一个类型错的条目。
    """
    r = _sync(client, {"skills": [], "agents": [_agent()]}).json()
    assert r["skipped"] == 1 and r["created"] == 0
    assert "包体" in r["outcomes"][0]["detail"] or "工号" in r["outcomes"][0]["detail"]


def test_a_rejected_entry_is_not_written(client, ivc_category):
    _sync(client, {"skills": [_skill(download_url="https://evil.example/x.tar.gz")], "agents": []})
    with SessionLocal() as db:
        assert db.get(RpAiSkill, "ivc-catt-ota-result-calculator") is None


def test_outsiders_cannot_sync_either(client, ivc_category):
    assert _sync(client, {"skills": [_skill()], "agents": []}, eid=OUTSIDER).status_code == 403
    with SessionLocal() as db:
        assert db.get(RpAiSkill, "ivc-catt-ota-result-calculator") is None


# --- 下载：先记事件，再转出去 -----------------------------------------------


def test_downloading_an_external_skill_records_then_redirects(client, ivc_category):
    """顺序是这里唯一有意义的决定：转出去之后门户什么都看不到了，所以记在前面。"""
    from app.models.download import RpAiSkillDownload

    _sync(client, {"skills": [_skill()], "agents": []})
    sid = "ivc-catt-ota-result-calculator"

    resp = client.get(
        f"/api/skills/{sid}/download", headers=_auth(ALLOWED), follow_redirects=False
    )
    assert resp.status_code == 307
    assert resp.headers["location"].startswith("https://arm1s10-eiffel239")

    with SessionLocal() as db:
        rows = db.query(RpAiSkillDownload).filter(
            RpAiSkillDownload.skill_id == sid
        ).all()
        assert [r.eid for r in rows] == [ALLOWED]
        db.query(RpAiSkillDownload).filter(
            RpAiSkillDownload.skill_id == sid
        ).delete(synchronize_session=False)
        db.commit()
