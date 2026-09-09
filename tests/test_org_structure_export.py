"""Exporting the report's org structure as JSON.

The one property worth defending: **this file and the HTML report describe the same
organisation.** Between the raw Graph payload and the page sit a stale-code
correction and a sub-unit merge, so an export that re-read the payload on its own
terms would differ from the page by a handful of people — a difference nobody could
explain, that no exception would report, and that would only ever surface as an
argument about whose number is right.

Hence the tests below do not check the export against the payload. They check it
against ``aggregate(level="big")``, which is the object the renderer prints the
department table from, and they check every count in the file against the members
listed beside it. Both were probed: rendering with ``require_enabled=True``, and
placing people by their own ``department`` instead of the corrected one, each break
a test here.

Access is the report's own: this names every individual and the team they sit in,
which is more than the page shows in one place.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.services.auth_tokens import create_token
from app.services.usage_report.departments import UNKNOWN
from app.services.usage_report.org_export import build_structure
from app.services.usage_report.render import aggregate, org_attribution

SECRET = "org-structure-export-test-secret"

# Shaped like the real snapshot: keyed by Graph object id, ``branch`` and ``depth``
# as the walk recorded them, reporting line in ``manager_id``. Five populations are
# represented on purpose, because each one is handled differently:
#
#   * the head, whose department has no sub-unit code at all;
#   * an ordinary team of two;
#   * a member whose account is switched off — in the tree, so counted, the way the
#     org chart in Teams counts them;
#   * a member carrying a stale fourth code (HWD3 is the former name of RD3), whom
#     the report places by their manager's department;
#   * a consultant, who is not Workforce and is not part of this population;
#   * somebody the walk reached with no department at all, which is the state of
#     thirteen real people who have no manager attribute.
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
    "id-gone": {
        "id": "id-gone",
        "onPremisesSamAccountName": "egone",
        "displayName": "Left Company",
        "jobTitle": "Developer SW",
        "department": "BNEW RTE RP SWD SW1 Software 1",
        "branch": "SWD",
        "depth": 2,
        "employeeType": "Workforce",
        "accountEnabled": False,
        "manager_id": "id-boss",
    },
    "id-rd3boss": {
        "id": "id-rd3boss",
        "onPremisesSamAccountName": "erd3boss",
        "displayName": "RD3 Boss",
        "jobTitle": "Head of Radio Design 3",
        "department": "BNEW RTE RP RD3 TIC Test Infra Center",
        "branch": "RD3",
        "depth": 1,
        "employeeType": "Workforce",
        "accountEnabled": True,
        "manager_id": "id-head",
    },
    "id-stale": {
        "id": "id-stale",
        "onPremisesSamAccountName": "estale",
        "displayName": "Stale Code Person",
        "jobTitle": "Engineer",
        # HWD3 no longer exists; the walk arrived here through RD3.
        "department": "BNEW RTE RP HWD3 TIC Test Infra",
        "branch": "RD3",
        "depth": 2,
        "employeeType": "Workforce",
        "accountEnabled": True,
        "manager_id": "id-rd3boss",
    },
    "id-consultant": {
        "id": "id-consultant",
        "onPremisesSamAccountName": "econsult",
        "displayName": "A Consultant",
        "jobTitle": "Consultant",
        "department": "BNEW RTE RP RD1 PA1 Power Amplifier 1",
        "branch": "RD1",
        "depth": 2,
        "employeeType": "Consultant",
        "accountEnabled": True,
        "manager_id": "id-head",
    },
    "id-nodept": {
        "id": "id-nodept",
        "onPremisesSamAccountName": "enodept",
        "displayName": "No Department",
        "jobTitle": "Engineer",
        "department": "",
        "branch": "",
        "depth": 1,
        "employeeType": "Workforce",
        "accountEnabled": True,
        "manager_id": "id-head",
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
    monkeypatch.setenv("DASHBOARD_EIDS", "EZHGMIO,ADMINMM")
    monkeypatch.setenv("USAGE_REPORT_ORG_SNAPSHOT", str(snapshot))
    monkeypatch.setenv("USAGE_REPORT_IDENTITY_SNAPSHOT", "")
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _headers(eid: str) -> dict[str, str]:
    token, _ = create_token(eid, display_name=eid.title())
    return {"Authorization": f"Bearer {token}"}


URL = "/api/department-report/org-structure.json"


def _structure(snapshot) -> dict:
    with SessionLocal() as db:
        return build_structure(db.connection(), snapshot)


def _all_members(structure: dict) -> list[dict]:
    return [
        member
        for dept in structure["departments"]
        for team in dept["teams"]
        for member in team["members"]
    ]


# --- access -----------------------------------------------------------------


def test_a_signed_out_caller_gets_nothing(client):
    assert client.get(URL).status_code == 401


def test_a_signed_in_colleague_is_still_refused(client):
    """It lists every individual and their team — the report's own restriction."""
    assert client.get(URL, headers=_headers("ESOMEONE")).status_code == 403


def test_a_configured_reader_gets_the_file(client):
    response = client.get(URL, headers=_headers("EZHGMIO"))
    assert response.status_code == 200
    assert response.json()["departments"]


# --- the file itself --------------------------------------------------------


def test_it_downloads_as_a_dated_file_rather_than_opening_in_the_tab(client):
    response = client.get(URL, headers=_headers("EZHGMIO"))
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    # The capture date, not today: two exports of one snapshot are the same
    # document, and two snapshots a month apart are not.
    day = response.json()["capturedAt"][:10]
    assert f'filename="rp-org-structure-{day}.json"' == disposition.split("; ", 1)[1]


def test_the_filename_header_is_readable_by_the_browser(client):
    """Without this the frontend cannot see it and saves a file called "download"."""
    exposed = client.get(URL, headers=_headers("EZHGMIO")).headers[
        "access-control-expose-headers"
    ]
    assert "Content-Disposition" in exposed


def test_chinese_labels_are_written_as_characters_not_escapes(client):
    """``ensure_ascii=True`` would turn every Chinese label into \\uXXXX.

    The file is read by people at least as often as by programs, and JSONResponse —
    the obvious choice for this endpoint — escapes by default.
    """
    body = client.get(URL, headers=_headers("EZHGMIO")).content.decode("utf-8")
    assert UNKNOWN in body
    assert "\\u" not in body


def test_no_snapshot_means_no_file_rather_than_an_invented_structure(
    client, monkeypatch
):
    monkeypatch.setenv("USAGE_REPORT_ORG_SNAPSHOT", "/nonexistent/graph_tree.json")
    get_settings.cache_clear()
    response = client.get(URL, headers=_headers("EZHGMIO"))
    assert response.status_code == 503
    assert "同步" in response.json()["detail"]


# --- the counts -------------------------------------------------------------


def test_every_count_matches_the_members_printed_beside_it(snapshot):
    """The drift guard.

    ``people`` is taken from the report's head counts and ``members`` from the
    report's placement — the same loop builds both, so they are equal by
    construction today. If that ever stops being true the file would still look
    perfectly fine, which is why it is asserted rather than assumed.
    """
    structure = _structure(snapshot)
    for dept in structure["departments"]:
        assert dept["people"] == sum(t["people"] for t in dept["teams"]), dept["code"]
        for team in dept["teams"]:
            assert team["people"] == len(team["members"]), team["code"]
    assert structure["totals"]["people"] == len(_all_members(structure))


def test_the_head_counts_are_the_ones_the_report_prints(snapshot):
    """Checked against the renderer's own input, not against the payload.

    ``aggregate(level="big")`` is the object the department table is printed from.
    Comparing against it is what makes "the file agrees with the page" a testable
    statement instead of an intention.
    """
    with SessionLocal() as db:
        conn = db.connection()
        structure = build_structure(conn, snapshot)
        from app.services.usage_report.render import load_org_tree

        tree = load_org_tree(conn, snapshot)
        placement, big_head, small_head, labels, _ = org_attribution(
            tree, set(), require_enabled=False
        )
        groups = aggregate(
            people={},
            metrics=[],
            big_head=big_head,
            small_head=small_head,
            placement=placement,
            labels=labels,
            level="big",
            in_scope=True,
        )
    assert {d["code"]: d["people"] for d in structure["departments"]} == {
        key: group.real for key, group in groups.items()
    }
    # And the names too: the page shows "PMO" for the code PO, and a file that
    # said "PO" would be a second vocabulary for the same reader.
    assert {d["code"]: d["name"] for d in structure["departments"]} == {
        key: group.label for key, group in groups.items()
    }


def test_a_switched_off_account_is_counted_and_labelled_not_dropped(snapshot):
    """The population is the reporting tree, which is how the org chart defines it.

    Probed by flipping ``require_enabled`` to True in ``build_structure``: this
    test and ``test_the_head_counts_are_the_ones_the_report_prints`` both fail,
    which is the point — dropping them here would make the file disagree with the
    page rather than raise anything.
    """
    structure = _structure(snapshot)
    gone = [m for m in _all_members(structure) if m["eid"] == "EGONE"]
    assert len(gone) == 1
    assert gone[0]["enabled"] is False
    assert structure["totals"]["disabled"] == 1
    # Counted in their team: three people in SW1, one of them switched off.
    swd = next(d for d in structure["departments"] if d["code"] == "SWD")
    assert swd["people"] == 3


def test_a_stale_department_code_is_corrected_the_way_the_report_corrects_it(snapshot):
    """HWD3 is the former name of RD3, and the units it names no longer exist.

    Probed by placing people from ``record["department"]`` instead of
    ``record["corrected"]``: this person lands under a department code the org
    chart no longer has, so the export grows a fifth department of one person and
    the head-count test fails too.
    """
    structure = _structure(snapshot)
    assert not [d for d in structure["departments"] if d["code"] == "HWD3"]
    rd3 = next(d for d in structure["departments"] if d["code"] == "RD3")
    tic = next(t for t in rd3["teams"] if t["code"] == "BNEW/RTE/RP/RD3/TIC")
    stale = next(m for m in tic["members"] if m["eid"] == "ESTALE")
    # Flagged, so a reader of the file can tell a placement this service reasoned
    # about from one that came straight from the directory.
    assert stale["staleDepartmentCode"] is True
    assert "HWD3" not in stale["department"]
    assert all(
        m["staleDepartmentCode"] is False
        for m in _all_members(structure)
        if m["eid"] != "ESTALE"
    )


def test_a_consultant_is_left_out_and_the_file_says_so(snapshot):
    """The lossy-export guard.

    Not Workforce, so not part of this population — but an export that is smaller
    than its snapshot has to say by how much, or it reads as a smaller
    organisation instead of as a filtered file.
    """
    structure = _structure(snapshot)
    assert "ECONSULT" not in {m["eid"] for m in _all_members(structure)}
    totals = structure["totals"]
    assert totals["inSnapshot"] == len(SNAPSHOT)
    assert totals["notWorkforce"] == 1
    assert totals["people"] == totals["inSnapshot"] - totals["notWorkforce"]


def test_somebody_with_no_department_is_placed_visibly_not_silently(snapshot):
    """Thirteen real people are in this state. They must not vanish, and they must

    not be filed under a real department either.
    """
    structure = _structure(snapshot)
    unknown = next(d for d in structure["departments"] if d["code"] == UNKNOWN)
    assert [m["eid"] for m in unknown["teams"][0]["members"]] == ["ENODEPT"]


# --- shape ------------------------------------------------------------------


def test_the_order_is_stable_so_two_exports_can_be_diffed(snapshot):
    """Largest unit first, as the report orders its tables, ties broken by code.

    A dict-insertion order would change with the Graph walk and make every diff
    between two months unreadable.
    """
    structure = _structure(snapshot)
    codes = [d["code"] for d in structure["departments"]]
    assert codes == ["SWD", "RD3", "PEU", UNKNOWN]
    for dept in structure["departments"]:
        sizes = [t["people"] for t in dept["teams"]]
        assert sizes == sorted(sizes, reverse=True)
        for team in dept["teams"]:
            eids = [m["eid"] for m in team["members"]]
            assert eids == sorted(eids)


def test_it_carries_when_the_directory_was_read_not_when_it_was_exported(snapshot):
    structure = _structure(snapshot)
    assert structure["capturedAt"]
    assert structure["generatedAt"]
    assert structure["source"] == "graph"
    assert structure["head"]["eid"] == "EHEAD"
    assert structure["head"]["name"] == "Head Person"


def test_a_member_carries_enough_to_be_useful_without_the_portal(snapshot):
    """Name and title included: a file of bare signums answers no question."""
    dev = next(m for m in _all_members(_structure(snapshot)) if m["eid"] == "EDEV")
    assert dev == {
        "eid": "EDEV",
        "name": "Dev Person",
        "title": "Developer SW",
        "department": "BNEW RTE RP SWD SW1 Software 1",
        "enabled": True,
        "staleDepartmentCode": False,
        "mergedSubunit": False,
    }
