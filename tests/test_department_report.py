"""The department usage report page: who may read it, and what it must not get wrong.

Two things are worth pinning down. Access first: the page names individuals and
shows what each of them uploaded and downloaded, so it is restricted the same way
the sign-in statistics are, and the sidebar hiding the link is not a control.

Then the head count. Every percentage in the report divides by the number of
people in the organisation, which comes from the Graph snapshot and nowhere else.
The failure mode worth a test is not an exception — it is the report quietly
falling back to counting portal rows, which would define adoption as 100% and
look entirely plausible.

The site-wide gate is switched ON here (the rest of the suite runs with
``AUTH_ENABLED=false``), because the endpoint reads the caller from the token.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.skill import RpAiSkill
from app.models.download import RpAiSkillDownload
from app.models.org_snapshot import RpAiOrgSnapshot
from app.models.user import RpAiPortal
from app.services.auth_tokens import create_token
from app.services.usage_report import graph_walk

SECRET = "usage-report-test-secret"

# Shaped like the real snapshot: keyed by Graph object id, with ``branch`` and
# ``depth`` as the tree walk recorded them and the reporting line in ``manager_id``.
# The head sits in their own unit (PEU in production), which is why the branch codes
# and the department strings have to be given rather than derived here.
SNAPSHOT = {
    "id-head": {
        "id": "id-head",
        "onPremisesSamAccountName": "ehead",
        "displayName": "Head Person",
        "jobTitle": "Head of Radio",
        "department": "BNEW RTE RP PEU Radio Pacific",
        "branch": "PEU",
        "depth": 0,
        "employeeType": "Workforce",
        "accountEnabled": True,
        "manager_id": None,
    },
    "id-boss": {
        "id": "id-boss",
        "onPremisesSamAccountName": "eboss",
        "displayName": "Team Boss",
        "jobTitle": "Head of Software Design",
        "department": "BNEW RTE RP SWD SW1 Software 1",
        "branch": "SWD",
        "depth": 1,
        "employeeType": "Workforce",
        "accountEnabled": True,
        "manager_id": "id-head",
    },
    "id-dev": {
        "id": "id-dev",
        "onPremisesSamAccountName": "edev",
        "displayName": "Dev Person",
        "jobTitle": "Developer SW",
        "department": "BNEW RTE RP SWD SW1 Software 1",
        "branch": "SWD",
        "depth": 2,
        "employeeType": "Workforce",
        "accountEnabled": True,
        "manager_id": "id-boss",
    },
    "id-quiet-head": {
        "id": "id-quiet-head",
        "onPremisesSamAccountName": "equiethead",
        "displayName": "Quiet Head",
        "jobTitle": "Head of Radio Design 1",
        "department": "BNEW RTE RP RD1 PA1 Power Amplifier 1",
        "branch": "RD1",
        "depth": 1,
        "employeeType": "Workforce",
        "accountEnabled": True,
        "manager_id": "id-head",
    },
    "id-quiet": {
        "id": "id-quiet",
        "onPremisesSamAccountName": "equiet",
        "displayName": "Quiet Person",
        "jobTitle": "Developer HW",
        "department": "BNEW RTE RP RD1 PA1 Power Amplifier 1",
        "branch": "RD1",
        "depth": 2,
        "employeeType": "Workforce",
        "accountEnabled": True,
        "manager_id": "id-quiet-head",
    },
}


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "graph_tree_cache.json"
    path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    return path


@pytest.fixture
def client(monkeypatch, snapshot):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", SECRET)
    monkeypatch.setenv("DASHBOARD_EIDS", "EZHGMIO,ADMINMM,EHUIWUU,EBAOXUE")
    monkeypatch.setenv("USAGE_REPORT_ORG_SNAPSHOT", str(snapshot))
    monkeypatch.setenv("USAGE_REPORT_IDENTITY_SNAPSHOT", "")
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _headers(eid: str) -> dict[str, str]:
    token, _ = create_token(eid, display_name=eid.title())
    return {"Authorization": f"Bearer {token}"}


def _seed_people() -> None:
    """Portal rows for the two people who have signed in, and one test row."""
    with SessionLocal() as db:
        db.add_all(
            [
                RpAiPortal(
                    eid="EDEV",
                    display_name="Dev Person",
                    department="BNEW RTE RP SWD SW1 Software 1",
                ),
                RpAiPortal(
                    eid="EBOSS",
                    display_name="Team Boss",
                    department="BNEW RTE RP SWD SW1 Software 1",
                ),
                # No name, no department: cannot be shown to be a person.
                RpAiPortal(eid="HELLO"),
            ]
        )
        db.commit()


# --- access -----------------------------------------------------------------


def test_a_signed_out_caller_gets_nothing(client):
    assert client.get("/api/department-report").status_code == 401


def test_a_signed_in_colleague_is_still_refused(client):
    """Being a portal user is not enough: the page names individuals."""
    response = client.get("/api/department-report", headers=_headers("ESOMEONE"))
    assert response.status_code == 403


@pytest.mark.parametrize("eid", ["EZHGMIO", "ADMINMM", "EHUIWUU", "EBAOXUE"])
def test_each_configured_reader_may_open_it(client, eid):
    _seed_people()
    response = client.get("/api/department-report", headers=_headers(eid))
    assert response.status_code == 200, response.text


def test_the_list_is_the_only_thing_that_grants_access(client, monkeypatch):
    """Drop an EID from the configuration and it loses access immediately."""
    monkeypatch.setenv("DASHBOARD_EIDS", "EZHGMIO")
    get_settings.cache_clear()
    assert client.get("/api/department-report", headers=_headers("EBAOXUE")).status_code == 403
    assert client.get("/api/department-report", headers=_headers("EZHGMIO")).status_code == 200


# --- the head count ---------------------------------------------------------


def test_without_the_org_snapshot_the_report_is_refused_not_estimated(
    client, monkeypatch
):
    """The denominator has one source. Losing it must not silently change the sums.

    Counting portal rows instead would make adoption 100% and read as a success.
    """
    monkeypatch.setenv("USAGE_REPORT_ORG_SNAPSHOT", "/nonexistent/graph_tree.json")
    get_settings.cache_clear()
    _seed_people()
    response = client.get("/api/department-report", headers=_headers("EZHGMIO"))
    assert response.status_code == 503
    assert "fetch_graph_tree" in response.json()["detail"]


def test_the_denominator_is_the_snapshot_not_the_number_of_portal_rows(client):
    _seed_people()
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    # Five people on the tree; two of them have used the portal.
    assert ">5</div>" in body
    assert "RP 组织在职人数" in body
    assert "其中用过门户" in body


def test_a_department_nobody_has_used_is_still_listed(client):
    """A department with no users is the finding, so it cannot be dropped."""
    _seed_people()
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert "Power Amplifier 1" in body


def test_an_account_with_neither_name_nor_department_is_not_counted_as_a_person(
    client,
):
    """A leftover test row must not inflate the number of users.

    Checked through the figure rather than through a list of excluded accounts,
    because that list is not printed any more — which is exactly why the figure
    is the thing worth pinning down.
    """
    with SessionLocal() as db:
        db.add_all(
            [
                RpAiPortal(
                    eid="EDEV",
                    display_name="Dev Person",
                    department="BNEW RTE RP SWD SW1 Software 1",
                ),
                RpAiPortal(eid="HELLO"),
                RpAiPortal(eid="DFADFA"),
            ]
        )
        db.commit()
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    users = re.search(r'<div class="n">(\d+)</div>\s*<div class="k">其中用过门户</div>', body)
    assert users and users.group(1) == "1", body[:400]


def test_a_service_account_that_uploaded_skills_is_flagged_as_not_a_person(client):
    """It has a display name, which is not the same as being in the directory.

    Reading a name as proof of personhood is what dropped this note once already:
    the bot's uploads stayed in the contributor table with nothing to say they
    were not somebody's work.
    """
    _seed_people()
    with SessionLocal() as db:
        db.add(RpAiPortal(eid="RP-AI-BOT", display_name="RP AI Bot"))
        db.flush()
        db.add(
            RpAiSkill(
                id="imported-skill",
                name="Imported Skill",
                category_id="public",
                uploader_eid="RP-AI-BOT",
                # Explicit: the report only counts skills that were really on the
                # market, and the model default is "pending". The official import
                # writes approved, so this is what a bot's skill actually looks like.
                status="approved",
            )
        )
        db.commit()
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert "RP-AI-BOT" in body
    assert "不是目录里的人" in body


# --- the page itself --------------------------------------------------------


def test_it_is_one_self_contained_page_with_no_outbound_requests(client):
    """Mailed on and opened from disk, so it must not need the network."""
    _seed_people()
    response = client.get("/api/department-report", headers=_headers("EZHGMIO"))
    body = response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in body
    assert "http://" not in body and "https://" not in body
    assert "<img" not in body and "<link" not in body


def test_the_numbers_are_not_cached_anywhere(client):
    """Generated per request precisely so they are current."""
    _seed_people()
    response = client.get("/api/department-report", headers=_headers("EZHGMIO"))
    assert response.headers["cache-control"] == "no-store"


def test_a_download_recorded_now_shows_up_now(client):
    """The whole point of generating on request rather than shipping a file."""
    _seed_people()
    with SessionLocal() as db:
        db.add(
            RpAiSkill(
                id="s1", name="S1", category_id="public", uploader_eid="EBOSS",
                status="approved",
            )
        )
        db.commit()
    before = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert "下载 skill" in before

    with SessionLocal() as db:
        db.add(RpAiSkillDownload(skill_id="s1", eid="EDEV"))
        db.commit()
    after = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert after != before


def test_every_chapter_can_be_folded_and_starts_open(client):
    _seed_people()
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert body.count('<details class="sec" open>') == body.count("</details>")
    assert body.count("<summary>") == body.count("</summary>")
    assert body.count("<summary>") == body.count("<h2")


# --- the snapshot in the database -------------------------------------------


def test_an_uploaded_snapshot_removes_the_need_for_a_file(client, monkeypatch):
    """The deployed case: no volume, no file, and the report still works."""
    monkeypatch.setenv("USAGE_REPORT_ORG_SNAPSHOT", "/nonexistent/graph_tree.json")
    get_settings.cache_clear()
    _seed_people()
    assert client.get("/api/department-report", headers=_headers("EZHGMIO")).status_code == 503

    uploaded = client.put(
        "/api/department-report/org-snapshot",
        json=SNAPSHOT,
        headers=_headers("EZHGMIO"),
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["people"] == len(SNAPSHOT)
    assert sorted(uploaded.json()["departments"]) == ["PEU", "RD1", "SWD"]

    response = client.get("/api/department-report", headers=_headers("EZHGMIO"))
    assert response.status_code == 200
    assert ">5</div>" in response.text


def test_the_uploaded_snapshot_wins_over_the_file(client, snapshot):
    """One organisation, one answer. A file left behind must not shadow it."""
    _seed_people()
    smaller = {k: v for k, v in SNAPSHOT.items() if k in {"id-head", "id-boss"}}
    client.put(
        "/api/department-report/org-snapshot", json=smaller, headers=_headers("EZHGMIO")
    )
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert ">2</div>" in body  # the upload, not the five in the file


def test_uploading_replaces_rather_than_accumulates(client):
    _seed_people()
    for _ in range(3):
        client.put(
            "/api/department-report/org-snapshot",
            json=SNAPSHOT,
            headers=_headers("EZHGMIO"),
        )
    with SessionLocal() as db:
        assert db.query(RpAiOrgSnapshot).count() == 1


def test_only_the_configured_readers_may_upload(client):
    """Whoever can replace this decides every head count in the report."""
    assert (
        client.put(
            "/api/department-report/org-snapshot",
            json=SNAPSHOT,
            headers=_headers("ESOMEONE"),
        ).status_code
        == 403
    )
    assert (
        client.put("/api/department-report/org-snapshot", json=SNAPSHOT).status_code
        == 401
    )


def test_a_snapshot_with_nobody_in_it_is_refused(client):
    response = client.put(
        "/api/department-report/org-snapshot", json={}, headers=_headers("EZHGMIO")
    )
    assert response.status_code == 422
    assert "没有人" in response.json()["detail"]


def test_a_snapshot_with_no_reporting_line_is_refused(client):
    """It would render — with head counts that are wrong and look fine."""
    flat = {
        key: {**rec, "branch": None}
        for key, rec in SNAPSHOT.items()
    }
    response = client.put(
        "/api/department-report/org-snapshot", json=flat, headers=_headers("EZHGMIO")
    )
    assert response.status_code == 422
    assert "branch" in response.json()["detail"]


def test_the_report_shows_when_the_directory_was_read_not_when_it_was_uploaded(
    client,
):
    """A week-old walk must not be presented as today's organisation.

    The footer is what a reader uses to judge how far the org structure may have
    moved since, so stamping the upload time would overstate how current it is.
    """
    _seed_people()
    uploaded = client.put(
        "/api/department-report/org-snapshot?capturedAt=2026-08-01T09:30:00",
        json=SNAPSHOT,
        headers=_headers("EZHGMIO"),
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["capturedAt"].startswith("2026-08-01T09:30")

    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert "2026-08-01 09:30" in body


# --- syncing from Graph on request ------------------------------------------


def test_sync_walks_graph_and_makes_the_result_the_report_structure(
    client, monkeypatch
):
    monkeypatch.setenv("USAGE_REPORT_ORG_SNAPSHOT", "/nonexistent/x.json")
    get_settings.cache_clear()
    _seed_people()
    assert client.get("/api/department-report", headers=_headers("EZHGMIO")).status_code == 503

    seen: dict[str, str] = {}

    def fake_walk(token: str, root_id: str, timeout: int = 120) -> dict:
        seen["token"] = token
        seen["root"] = root_id
        return SNAPSHOT

    monkeypatch.setattr(graph_walk, "walk", fake_walk)
    response = client.post(
        "/api/department-report/org-snapshot/refresh",
        json={"token": "a-graph-token"},
        headers=_headers("EZHGMIO"),
    )
    assert response.status_code == 200, response.text
    assert seen["token"] == "a-graph-token"
    assert response.json()["people"] == len(SNAPSHOT)
    assert client.get("/api/department-report", headers=_headers("EZHGMIO")).status_code == 200


def test_an_expired_token_says_so_and_keeps_the_old_structure(client, monkeypatch):
    """The reader would rather see last week's departments than none."""
    _seed_people()
    client.put(
        "/api/department-report/org-snapshot", json=SNAPSHOT, headers=_headers("EZHGMIO")
    )

    def rejected(token: str, root_id: str, timeout: int = 120) -> dict:
        raise graph_walk.GraphTokenRejected("nope")

    monkeypatch.setattr(graph_walk, "walk", rejected)
    response = client.post(
        "/api/department-report/org-snapshot/refresh",
        json={"token": "stale"},
        headers=_headers("EZHGMIO"),
    )
    assert response.status_code == 400
    assert "过期" in response.json()["detail"]
    # Still there, still usable.
    with SessionLocal() as db:
        assert db.query(RpAiOrgSnapshot).count() == 1
    assert client.get("/api/department-report", headers=_headers("EZHGMIO")).status_code == 200


def test_a_walk_that_breaks_partway_does_not_replace_anything(client, monkeypatch):
    _seed_people()
    client.put(
        "/api/department-report/org-snapshot", json=SNAPSHOT, headers=_headers("EZHGMIO")
    )
    before = client.get("/api/department-report", headers=_headers("EZHGMIO")).text

    def broken(token: str, root_id: str, timeout: int = 120) -> dict:
        raise graph_walk.GraphWalkFailed("$batch failed after 4 attempts: HTTP 429")

    monkeypatch.setattr(graph_walk, "walk", broken)
    response = client.post(
        "/api/department-report/org-snapshot/refresh",
        json={"token": "x"},
        headers=_headers("EZHGMIO"),
    )
    assert response.status_code == 502
    assert "保持不变" in response.json()["detail"]
    after = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    # Same organisation as before: only the generation timestamp may differ.
    assert after.count("<h2") == before.count("<h2")


def test_sync_needs_a_token(client):
    response = client.post(
        "/api/department-report/org-snapshot/refresh",
        json={},
        headers=_headers("EZHGMIO"),
    )
    assert response.status_code == 422
    assert "Graph access token" in response.json()["detail"]


def test_only_the_configured_readers_may_sync(client):
    assert (
        client.post(
            "/api/department-report/org-snapshot/refresh",
            json={"token": "x"},
            headers=_headers("ESOMEONE"),
        ).status_code
        == 403
    )


def test_the_page_can_report_which_structure_it_is_using(client, monkeypatch):
    monkeypatch.setenv("USAGE_REPORT_ORG_SNAPSHOT", "/nonexistent/x.json")
    get_settings.cache_clear()
    empty = client.get(
        "/api/department-report/org-snapshot", headers=_headers("EZHGMIO")
    ).json()
    assert empty["present"] is False

    client.put(
        "/api/department-report/org-snapshot?capturedAt=2026-08-20T08:00",
        json=SNAPSHOT,
        headers=_headers("EZHGMIO"),
    )
    got = client.get(
        "/api/department-report/org-snapshot", headers=_headers("EZHGMIO")
    ).json()
    assert got == {
        "present": True,
        "source": "database",
        "capturedAt": "2026-08-20T08:00",
        "people": len(SNAPSHOT),
        "uploadedBy": "EZHGMIO",
    }


# --- what a cell in the table actually says ---------------------------------
#
# 这一组存在的理由是一次真实的误读:一个人上传了三个 agent,而"上传 agent"那一格显示
# 1 —— 那是人数。列标题写着"上传 agent"、指标单位写着"个 agent",于是读的人合理地把 1
# 读成"一个 agent",看不出这个部门交了三个。改之前那 29 个测试全过,因为**没有一个测试
# 断言过格子里显示什么**,这正是它能上线的原因。


def _cell(body: str, column_label: str, row_label: str) -> str:
    """从渲染出来的大部门表里取一个格子的文本。

    按表头的位置找列,而不是按写死的下标:列的顺序会变,而"第 8 个 td"这种断言在顺序变
    了之后仍然通过,只是它测的已经是另一列了。
    """
    table = re.search(r'<table class="sortable">.*?</table>', body, re.S).group(0)
    head = re.search(r"<thead>(.*?)</thead>", table, re.S).group(1)
    headers = [
        " ".join(re.sub(r"<[^>]+>", " ", th).split())
        for th in re.findall(r"<th[^>]*>(.*?)</th>", head, re.S)
    ]
    idx = next(i for i, h in enumerate(headers) if h.startswith(column_label))
    for row in re.findall(r"<tr>(.*?)</tr>", table, re.S):
        cells = [
            " ".join(re.sub(r"<[^>]+>", " ", td).split())
            for td in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        ]
        if cells and cells[0] == row_label:
            return cells[idx]
    raise AssertionError(f"表里没有 {row_label} 这一行")


def _seed_market_agents(creator: str, how_many: int) -> None:
    from app.models.market import RpAiMarketItem

    with SessionLocal() as db:
        for n in range(how_many):
            db.add(
                RpAiMarketItem(
                    id=f"agent-{creator.lower()}-{n}",
                    name=f"Agent {n}",
                    item_type="agent",
                    service_type="code",
                    source="upload",
                    category_id="public",
                    creator=creator,
                    status="approved",
                )
            )
        db.commit()


def test_three_agents_from_one_person_do_not_read_as_one(client):
    """一个人交三个,格子要说 3,不能只说"有一个人交过"。

    这是这一列的全部价值:同一个人交第二个、第三个 agent 时,一个只数人的列一个数字都
    不会变,而"传了但报告没动"看起来完全像一个 bug。
    """
    _seed_people()
    _seed_market_agents("EDEV", 3)
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert _cell(body, "上传 agent", "SWD") == "1 / 3"


def test_one_item_still_shows_both_numbers(client):
    """人数与件数相等时也要两个都写。

    原来相等就省掉后缀,于是同一列里的 "1" 有两种意思(一人一件、一人多件),而读者分不出
    是哪一种。省掉的那点噪音换来的是一列有两种读法。
    """
    _seed_people()
    _seed_market_agents("EDEV", 1)
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert _cell(body, "上传 agent", "SWD") == "1 / 1"


def test_a_zero_stays_a_single_zero(client):
    """0 人必然 0 件,没有歧义,而这张表里零很多。"""
    _seed_people()
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert _cell(body, "上传 agent", "SWD") == "0"


def test_uploaded_skills_are_counted_the_same_way(client):
    """skill 和 agent 是同一个口径。

    两列曾经都只显示人数,但同表的另外几列显示「人数 / 次数」,于是看起来像 skill 那边有
    件数、agent 这边没有 —— 两列一起改,免得再长出第二种读法。
    """
    _seed_people()
    with SessionLocal() as db:
        db.add_all(
            [
                RpAiSkill(id="s-a", name="A", category_id="public", uploader_eid="EDEV",
                          status="approved"),
                RpAiSkill(id="s-b", name="B", category_id="public", uploader_eid="EDEV",
                          status="approved"),
            ]
        )
        db.commit()
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert _cell(body, "上传 skill", "SWD") == "1 / 2"


def test_every_column_says_how_to_read_it(client):
    """表头写出这一列是人数还是「人数 / 件数」。

    两种形态并存而表头不说,读者只能猜 —— 这次就是这么猜错的。
    """
    _seed_people()
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    table = re.search(r'<table class="sortable">.*?</table>', body, re.S).group(0)
    head = re.search(r"<thead>(.*?)</thead>", table, re.S).group(1)
    for label, how in [
        ("上传 agent", "人数 / 个 agent"),
        ("上传 skill", "人数 / 个 skill"),
        ("近期登录", "人数 / 次登录"),
    ]:
        th = next(
            t for t in re.findall(r"<th[^>]*>(.*?)</th>", head, re.S) if label in t
        )
        assert how in " ".join(re.sub(r"<[^>]+>", " ", th).split()), (label, th)


def test_the_appendix_counts_items_not_times(client):
    """附录里用指标自己的单位,不写死「次」。

    "另有 1 个非个人账号、共 7 次" 读起来像它下载了 7 次,而实际是它交了 7 个 skill。
    """
    _seed_people()
    with SessionLocal() as db:
        db.add(RpAiPortal(eid="RP-AI-BOT", display_name="RP AI Bot"))
        db.flush()
        db.add_all(
            [
                RpAiSkill(
                    id=f"bot-{n}",
                    name=f"Bot {n}",
                    category_id="public",
                    uploader_eid="RP-AI-BOT",
                    status="approved",
                )
                for n in range(2)
            ]
        )
        db.commit()
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert "共 2 个 skill，未计入" in body
    assert "共 2 次，未计入" not in body


# --- language ---------------------------------------------------------------
#
# Skeleton only, on purpose. The last test in this block pins the half that is
# *not* translated, so that "the 口径 paragraphs are still Chinese" reads as a
# decision rather than as an unfinished job.


def test_without_a_language_the_report_is_chinese(client):
    """An existing bookmark must not change meaning when the switch ships."""
    _seed_people()
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert "RP AI Portal 使用情况报告" in body
    assert '<html lang="zh-CN">' in body


def test_asking_for_english_translates_the_skeleton(client):
    _seed_people()
    body = client.get(
        "/api/department-report?lang=en", headers=_headers("EZHGMIO")
    ).text
    assert '<html lang="en">' in body
    # Headings, KPI labels, table headers, metric names.
    for wanted in (
        "RP AI Portal usage report",
        "1. Departments (second level) compared",
        "4. Contributors: who made things, and whose things get used",
        "Appendix",
        "People in RP",
        "Of those, have used the portal",
        "Adoption",
        "Skill downloads",
    ):
        assert wanted in body, wanted
    # And the Chinese skeleton is gone. Checked in its tag-delimited form, because
    # several of these words legitimately survive inside the 口径 prose that this
    # feature deliberately leaves in Chinese — asserting on the bare word would
    # fail for the right behaviour. (``渗透率`` in the note above the cards is
    # exactly that case.)
    for gone in (
        "<h1>RP AI Portal 使用情况报告</h1>",
        "<h2>一、大部门（二级部门）对比</h2>",
        ">RP 组织在职人数<",
        ">渗透率<",
        '<td class="l">合计</td>',
    ):
        assert gone not in body, gone


def test_the_english_page_has_no_chinese_wording_left_in_it(client):
    """``lang=en`` means the whole page, prose included.

    This replaces an earlier test that pinned the opposite — skeleton translated,
    口径 paragraphs left Chinese. That scope was deliberate at the time and has
    been superseded, so the test was inverted rather than deleted: the assertion
    that the page is *not* half-translated is the one worth keeping.

    Asserted as "no CJK anywhere" rather than by listing paragraphs, because a
    list only covers the paragraphs somebody remembered. The fixtures' own data
    (departments, names) is all ASCII, so any CJK found here is untranslated
    wording rather than Chinese content. Real data can of course contain Chinese
    names, and those are data — this test is about the template.
    """
    _seed_people()
    body = client.get(
        "/api/department-report?lang=en", headers=_headers("EZHGMIO")
    ).text
    leftover = re.findall(r"[\u4e00-\u9fff]+", body)
    assert not leftover, f"untranslated wording on the English page: {leftover[:10]}"


def test_the_english_page_really_does_carry_the_translated_prose(client):
    """The counterpart to the check above, which an empty page would also pass."""
    _seed_people()
    body = client.get(
        "/api/department-report?lang=en", headers=_headers("EZHGMIO")
    ).text
    assert "The headers sort." in body
    assert "are not the same thing" in body
    assert "is not the sum of the columns to its right" in body
    assert "Each entry states what it actually counts" in body


def test_the_chinese_page_still_carries_the_chinese_prose(client):
    """The prose moved out of the template into the wording table.

    Pinned because that move is exactly the kind of refactor that can drop a
    paragraph without any other test noticing.
    """
    _seed_people()
    body = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert "点表头可排序" in body
    assert "「用过门户」和「近期登录」不是同一件事" in body
    assert "口径写在这里而不是脚注" in body


def test_an_unknown_language_is_refused_rather_than_silently_chinese(client):
    _seed_people()
    r = client.get("/api/department-report?lang=eng", headers=_headers("EZHGMIO"))
    assert r.status_code == 422
    assert "eng" in r.json()["detail"]


def test_the_language_switch_does_not_change_any_number(client):
    """Same query, same figures — only the wording may differ."""
    _seed_people()
    zh = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    en = client.get(
        "/api/department-report?lang=en", headers=_headers("EZHGMIO")
    ).text
    # The head-count card is the number every percentage in the report divides by.
    assert ">5</div>" in zh and ">5</div>" in en


def test_no_downloads_yet_shows_a_dash_not_the_word_none(client):
    """The fixtures have no downloads, which is exactly the empty-portal case.

    ``first_seen`` is None then, and interpolating it straight into the KPI card
    gave "since None" / "None 起". Pinned in both languages because the card is
    built from one template.

    Scoped to the card on purpose: the same value is also interpolated into the
    Chinese ``口径`` paragraphs ("登录明细表上线的 None 之后"), which this change
    does not touch. That is a pre-existing wart on ``main``, only reachable with an
    empty database, and asserting on the whole page would fail for it instead.
    """
    _seed_people()
    zh = client.get("/api/department-report", headers=_headers("EZHGMIO")).text
    assert "None 起" not in zh
    assert "—" in zh
    en = client.get(
        "/api/department-report?lang=en", headers=_headers("EZHGMIO")
    ).text
    assert "since None" not in en
    assert "since —" in en
