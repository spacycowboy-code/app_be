"""``rp_ai_portal.origin``: an account row that is not a sign-in.

A bulk import registers work by people who have never opened the portal. Those
people need an account row — the card's org-group badge and the report's
department placement both read ``rp_ai_portal.department`` — but the same table
is what the report counts to answer "how many people have used the portal".

The whole point of the column is that one insert must not answer both questions
the same way. These tests pin the split in both directions, because getting
either half wrong is silent:

* count them as users and the penetration rate goes up on the strength of people
  who never signed in;
* drop them from ``build_people`` and their assets land in "无法归属" instead of
  in their department.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.market import RpAiMarketItem
from app.models.user import RpAiPortal
from app.services.auth_tokens import create_token

SECRET = "portal-origin-test-secret"

#: Two people in one department, so a filter that drops the wrong one is visible
#: in the head count rather than only in a percentage.
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
    "id-signed-in": {
        "id": "id-signed-in",
        "onPremisesSamAccountName": "esignin",
        "displayName": "Signed In Person",
        "jobTitle": "Developer SW",
        "department": "BNEW RTE RP SWD SW1 Software 1",
        "branch": "SWD",
        "depth": 2,
        "employeeType": "Workforce",
        "accountEnabled": True,
        "manager_id": "id-boss",
    },
    "id-imported": {
        "id": "id-imported",
        "onPremisesSamAccountName": "eimport",
        "displayName": "Imported Person",
        "jobTitle": "Developer SW",
        "department": "BNEW RTE RP SWD SW1 Software 1",
        "branch": "SWD",
        "depth": 2,
        "employeeType": "Workforce",
        "accountEnabled": True,
        "manager_id": "id-boss",
    },
}

SWD = "BNEW RTE RP SWD SW1 Software 1"


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "graph_tree_cache.json"
    path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    return path


@pytest.fixture
def client(monkeypatch, snapshot):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", SECRET)
    monkeypatch.setenv("DASHBOARD_EIDS", "EZHGMIO")
    monkeypatch.setenv("USAGE_REPORT_ORG_SNAPSHOT", str(snapshot))
    monkeypatch.setenv("USAGE_REPORT_IDENTITY_SNAPSHOT", "")
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _headers(eid: str = "EZHGMIO") -> dict[str, str]:
    token, _ = create_token(eid, display_name=eid.title())
    return {"Authorization": f"Bearer {token}"}


def _report(client) -> str:
    response = client.get("/api/department-report", headers=_headers())
    assert response.status_code == 200, response.text
    return response.text


def _seed(with_import: bool) -> None:
    """One person who signed in; optionally one registered by an import.

    The imported person owns an agent, which is the situation the column exists
    for: the asset is real, the sign-in is not.
    """
    with SessionLocal() as db:
        db.add(
            RpAiPortal(eid="ESIGNIN", display_name="Signed In Person", department=SWD)
        )
        if with_import:
            db.add(
                RpAiPortal(
                    eid="EIMPORT",
                    display_name="Imported Person",
                    department=SWD,
                    origin="import",
                )
            )
            db.add(
                RpAiMarketItem(
                    id="imported-agent",
                    name="Imported Agent",
                    item_type="agent",
                    service_type="cloud",
                    service_url="https://example.invalid/agent/",
                    source="manual",
                    creator="EIMPORT",
                    category_id="radio-sw",
                )
            )
        db.commit()


# --- the column itself ------------------------------------------------------


def test_a_row_created_the_normal_way_is_a_sign_in():
    """The default has to be 'login', or the existing rows change meaning.

    The column is added to a populated table without a backfill, and that is only
    sound because every row that predates it was written on a first sign-in.
    """
    with SessionLocal() as db:
        db.add(RpAiPortal(eid="EPLAIN", display_name="Plain", department=SWD))
        db.commit()
        row = db.query(RpAiPortal).filter(RpAiPortal.eid == "EPLAIN").one()
        assert row.origin == "login"


def test_signing_in_does_not_rewrite_an_imported_row(client):
    """An import row must survive its owner's first real sign-in as a login row.

    Not a cosmetic point: leaving it at 'import' forever would keep that person
    out of the penetration rate after they actually started using the portal.
    """
    from app.routers import auth as auth_router

    with SessionLocal() as db:
        db.add(
            RpAiPortal(
                eid="EIMPORT", display_name="Imported", department=SWD, origin="import"
            )
        )
        db.commit()

    with SessionLocal() as db:
        auth_router._upsert_user(
            db,
            type("U", (), {"eid": "EIMPORT", "display_name": "Imported", "department": SWD})(),
        )
        db.commit()
        row = db.query(RpAiPortal).filter(RpAiPortal.eid == "EIMPORT").one()
        assert row.origin == "login"


# --- what the report must not say -------------------------------------------


def test_an_imported_row_is_not_counted_as_having_used_the_portal(client):
    """The head count and the penetration rate stay put.

    Compared against the same report without the import rather than against a
    hard-coded number, so the assertion survives changes to the fixture.
    """
    _seed(with_import=False)
    without = _report(client)

    with SessionLocal() as db:
        db.query(RpAiPortal).delete()
        db.query(RpAiMarketItem).delete()
        db.commit()
    _seed(with_import=True)
    with_import = _report(client)

    # "其中用过门户" — the number in that card, and the sentence quoting it.
    assert '<div class="n">1</div><div class="k">其中用过门户</div>' in without
    assert '<div class="n">1</div><div class="k">其中用过门户</div>' in with_import
    assert "RP 内 1 人登录过" in with_import


def test_the_metric_row_for_first_logins_excludes_imported_rows(client):
    """``ever_signed_in`` is literally captioned 曾登录过的人数."""
    _seed(with_import=True)
    body = _report(client)
    assert "曾登录过的人数" in body
    assert "导入登记的账号行不算在内" in body


# --- what the report must still say -----------------------------------------


def test_an_imported_person_still_places_their_asset_in_their_department(client):
    """The reason for inserting the row at all.

    Without the row the agent's creator is unknown to ``build_people`` and the
    item is reported as 无法归属 instead of counted under SWD.
    """
    _seed(with_import=True)
    body = _report(client)
    assert "Imported Person" in body
    # The agent is attributed, so there is nothing left over to report as
    # unattributable for the upload metric.
    assert "1 条无法归属到人" not in body


def test_dropping_imported_rows_from_people_would_lose_the_asset(client):
    """Guard the other half: prove the assertion above can fail.

    If ``build_people`` ever starts filtering on origin, the item becomes
    unattributed. Pinning it from the outside is the only way this stays visible,
    because nothing raises.
    """
    from app.services.usage_report import render as render_mod

    _seed(with_import=True)
    people, _, _ = render_mod.build_people(
        SessionLocal().connection(),
        {"people": {k: v for k, v in SNAPSHOT.items()}},
        {},
        "",
    )
    assert "EIMPORT" in people, "an imported row is still a person"
    assert people["EIMPORT"].signed_in is False
    assert people["ESIGNIN"].signed_in is True


# --- the registration tool --------------------------------------------------


@pytest.fixture
def fake_directory(monkeypatch):
    """Stand in for LDAP so the tool's decisions can be tested without a bind."""
    from tools import register_directory_users as tool

    monkeypatch.setattr(
        tool,
        "lookup",
        lambda eids: {
            "ENEW": {"name": "New Person", "department": SWD},
            "EOLD": {"name": "Old Person", "department": SWD},
            "ENODEPT": {"name": "Contractor (EXT)", "department": ""},
        },
    )
    return tool


def _run(tool, monkeypatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["register_directory_users.py", *argv])
    return tool.main()


def test_a_dry_run_writes_nothing(fake_directory, monkeypatch):
    assert _run(fake_directory, monkeypatch, "ENEW") == 0
    with SessionLocal() as db:
        assert db.query(RpAiPortal).filter(RpAiPortal.eid == "ENEW").count() == 0


def test_apply_creates_the_row_marked_as_an_import(fake_directory, monkeypatch):
    assert _run(fake_directory, monkeypatch, "--apply", "enew") == 0
    with SessionLocal() as db:
        row = db.query(RpAiPortal).filter(RpAiPortal.eid == "ENEW").one()
        assert row.origin == "import"
        assert row.department == SWD
        assert row.display_name == "New Person"


def test_an_existing_row_is_never_rewritten(fake_directory, monkeypatch):
    """Re-running must not turn a real sign-in into an import.

    The tool is the kind of thing that gets run twice, and demoting a genuine
    user would quietly take them out of the penetration rate.
    """
    with SessionLocal() as db:
        db.add(RpAiPortal(eid="EOLD", display_name="Old Person", department=SWD))
        db.commit()
    assert _run(fake_directory, monkeypatch, "--apply", "EOLD") == 0
    with SessionLocal() as db:
        row = db.query(RpAiPortal).filter(RpAiPortal.eid == "EOLD").one()
        assert row.origin == "login"
        assert db.query(RpAiPortal).filter(RpAiPortal.eid == "EOLD").count() == 1


def test_someone_with_no_department_is_skipped(fake_directory, monkeypatch, capsys):
    """A row with no department buys nothing — say so instead of writing it.

    The badge needs the department and so does the report's placement, so such a
    row would look registered and still show blank.
    """
    assert _run(fake_directory, monkeypatch, "--apply", "ENODEPT") == 0
    assert "no department" in capsys.readouterr().out
    with SessionLocal() as db:
        assert db.query(RpAiPortal).filter(RpAiPortal.eid == "ENODEPT").count() == 0
