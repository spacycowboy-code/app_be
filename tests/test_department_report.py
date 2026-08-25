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
            RpAiSkill(id="s1", name="S1", category_id="public", uploader_eid="EBOSS")
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
