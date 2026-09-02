"""Usage recording, personal history, and the weekly report.

Three properties are worth pinning down, because each of them is a mistake this
codebase has already made once somewhere else:

* **Attribution comes from the session.** The upload endpoint used to take the
  actor from the request body, which made the audit trail forgeable
  (``test_upload_actor.py``). A usage log with the same flaw would be worse: it
  is the input to a report.
* **A person's history is theirs.** The older ``/api/users/{eid}/*`` endpoints
  let any signed-in user read anyone's data. Download history and tool use are
  a record of how a colleague works, so these two check the caller.
* **The report names no individuals.** Not a style preference: a per-person
  ranking would make sign-ins and tool runs worth gaming, and the numbers would
  stop measuring adoption. The test asserts the absence of EIDs in the payload
  so that adding one has to be a deliberate act that breaks a test.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.audit import RpAiSkillAuditRecord
from app.models.download import RpAiSkillDownload
from app.models.login_event import RpAiLoginEvent
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.skill import RpAiSkill
from app.models.usage_event import RpAiUsageEvent
from app.models.user import RpAiPortal
from app.services.auth_tokens import create_token
from app.services.reporting_windows import current_week, db_now

ME = "EZHGMIO"          # also a dashboard EID, per the default settings
COLLEAGUE = "EOTHER"
DASHBOARD_ONLY = "ADMINMM"


@pytest.fixture()
def client(monkeypatch):
    """A client with the auth gate on, so ``request.state.eid`` is populated.

    The default conftest turns the gate off, which would leave every endpoint
    here without an identity — and identity is the thing being tested.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "usage-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    return TestClient(app)


@pytest.fixture()
def db():
    with SessionLocal() as session:
        yield session


def _auth(eid: str) -> dict:
    token, _ = create_token(eid)
    return {"Authorization": f"Bearer {token}"}


def _run_event(**kw) -> dict:
    body = {
        "verb": "tool_run",
        "objectType": "tool",
        "objectId": "product-structure",
        "objectName": "Product Structure Information",
        "source": "tool-page",
        "outcome": "ok",
        "durationMs": 1200,
    }
    body.update(kw)
    return body


# --- Recording --------------------------------------------------------------


def test_the_actor_is_the_session_not_a_field_in_the_body(client, db):
    """An eid in the payload is ignored; the token decides who acted."""
    resp = client.post(
        "/api/usage/events",
        json=_run_event(eid=COLLEAGUE),
        headers=_auth(ME),
    )

    assert resp.status_code == 204
    events = db.query(RpAiUsageEvent).all()
    assert [e.eid for e in events] == [ME]


def test_the_department_is_snapshotted_from_the_user_row(client, db):
    db.add(RpAiPortal(eid=ME, department="BNEW RTE RP PS AI"))
    db.commit()

    client.post("/api/usage/events", json=_run_event(), headers=_auth(ME))

    assert db.query(RpAiUsageEvent).one().department == "BNEW RTE RP PS AI"


def test_an_anonymous_caller_cannot_record(client):
    assert client.post("/api/usage/events", json=_run_event()).status_code == 401


def test_an_unknown_verb_is_refused(client, db):
    resp = client.post(
        "/api/usage/events", json=_run_event(verb="mouse_moved"), headers=_auth(ME)
    )

    assert resp.status_code == 422
    assert db.query(RpAiUsageEvent).count() == 0


def test_a_run_without_an_outcome_is_refused(client):
    """A run with no outcome would sit in a success-rate denominator as neither."""
    resp = client.post(
        "/api/usage/events",
        json=_run_event(outcome=None, durationMs=None),
        headers=_auth(ME),
    )

    assert resp.status_code == 422


def test_an_open_cannot_claim_an_outcome(client):
    """Opens are launches: the portal never sees how they ended (see the model)."""
    resp = client.post(
        "/api/usage/events",
        json=_run_event(verb="tool_open", objectType="market_item", objectId="gl-7"),
        headers=_auth(ME),
    )

    assert resp.status_code == 422


def test_an_absurd_duration_is_refused_rather_than_averaged_in(client):
    """A suspended tab can report hours; one such row would wreck any average."""
    resp = client.post(
        "/api/usage/events",
        json=_run_event(durationMs=99_999_999),
        headers=_auth(ME),
    )

    assert resp.status_code == 422


# --- Personal history: who may read it --------------------------------------


def test_a_colleague_cannot_read_someone_elses_download_history(client):
    resp = client.get(f"/api/users/{ME}/downloads", headers=_auth(COLLEAGUE))

    assert resp.status_code == 403
    assert "your own" in resp.json()["detail"].lower()


def test_a_colleague_cannot_read_someone_elses_tools(client):
    assert client.get(f"/api/users/{ME}/tools", headers=_auth(COLLEAGUE)).status_code == 403


def test_you_can_read_your_own(client):
    assert client.get(f"/api/users/{COLLEAGUE}/downloads", headers=_auth(COLLEAGUE)).status_code == 200


def test_a_dashboard_eid_may_read_anothers_history(client):
    """Deliberate: the same people who can already see the sign-in log."""
    resp = client.get(f"/api/users/{COLLEAGUE}/downloads", headers=_auth(DASHBOARD_ONLY))

    assert resp.status_code == 200


# --- Personal history: content ----------------------------------------------


def test_downloads_merge_both_kinds_newest_first(client, db):
    now = db_now()
    db.add_all(
        [
            RpAiSkill(id="s1", name="Skill One", category_id="public", version="1.2.0"),
            RpAiMarketItem(
                id="gl-9", name="Item Nine", item_type="tool", service_type="code",
                version="0.4.0",
            ),
        ]
    )
    db.commit()
    db.add_all(
        [
            RpAiSkillDownload(skill_id="s1", eid=ME, created_at=now - timedelta(hours=2)),
            RpAiMarketDownload(item_id="gl-9", eid=ME, created_at=now - timedelta(hours=1)),
        ]
    )
    db.commit()

    body = client.get(f"/api/users/{ME}/downloads", headers=_auth(ME)).json()

    assert [(e["objectType"], e["objectId"]) for e in body] == [
        ("market_item", "gl-9"),
        ("skill", "s1"),
    ]
    assert body[1]["name"] == "Skill One"
    assert body[1]["version"] == "1.2.0"


def test_downloads_show_only_your_own(client, db):
    db.add(RpAiSkill(id="s1", name="Skill One", category_id="public"))
    db.commit()
    db.add_all(
        [
            RpAiSkillDownload(skill_id="s1", eid=ME),
            RpAiSkillDownload(skill_id="s1", eid=COLLEAGUE),
        ]
    )
    db.commit()

    body = client.get(f"/api/users/{ME}/downloads", headers=_auth(ME)).json()

    assert len(body) == 1


def test_runs_and_opens_are_counted_separately_not_summed(client, db):
    """The whole point of two verbs: they are different evidence."""
    now = db_now()
    db.add_all(
        [
            RpAiUsageEvent(
                eid=ME, verb="tool_run", object_type="tool", object_id="ps",
                object_name="PS", outcome="ok", created_at=now,
            ),
            RpAiUsageEvent(
                eid=ME, verb="tool_run", object_type="tool", object_id="ps",
                outcome="error", created_at=now,
            ),
            RpAiUsageEvent(
                eid=ME, verb="tool_open", object_type="tool", object_id="ps",
                created_at=now,
            ),
        ]
    )
    db.commit()

    body = client.get(f"/api/users/{ME}/tools", headers=_auth(ME)).json()

    assert len(body) == 1
    assert body[0]["runs"] == 2
    assert body[0]["opens"] == 1
    assert body[0]["failedRuns"] == 1


def test_tools_outside_the_window_are_left_out(client, db):
    db.add(
        RpAiUsageEvent(
            eid=ME, verb="tool_run", object_type="tool", object_id="old",
            outcome="ok", created_at=db_now() - timedelta(days=90),
        )
    )
    db.commit()

    assert client.get(f"/api/users/{ME}/tools?days=30", headers=_auth(ME)).json() == []


# --- The weekly report ------------------------------------------------------


def test_the_report_is_restricted(client):
    assert client.get("/api/usage-report", headers=_auth(COLLEAGUE)).status_code == 403


def test_the_report_names_no_individuals(client, db):
    """Guards the design decision: aggregates only, no per-person rows.

    Asserted on the serialized payload rather than on a schema, so adding a
    field that carries an EID fails here even if it is nested.
    """
    last_week = current_week(weeks_back=1)
    when = last_week.since + timedelta(hours=1)
    db.add(
        RpAiSkill(
            id="s1", name="Skill One", category_id="public", created_at=when,
            # Explicit: the model defaults to ``pending``, and the report counts only
            # what is on the market. Left implicit this row is an unreviewed upload.
            status="approved",
        )
    )
    db.commit()
    db.add_all(
        [
            RpAiLoginEvent(eid=ME, created_at=when),
            RpAiLoginEvent(eid=COLLEAGUE, created_at=when),
            RpAiSkillDownload(skill_id="s1", eid=ME, created_at=when),
            RpAiUsageEvent(
                eid=COLLEAGUE, verb="tool_run", object_type="tool", object_id="ps",
                outcome="ok", created_at=when,
            ),
        ]
    )
    db.commit()

    raw = client.get("/api/usage-report", headers=_auth(ME)).text

    assert ME not in raw
    assert COLLEAGUE not in raw


def test_the_report_counts_people_and_assets_for_the_last_full_week(client, db):
    last_week = current_week(weeks_back=1)
    when = last_week.since + timedelta(hours=1)
    db.add(
        RpAiSkill(
            id="s1", name="Skill One", category_id="public", created_at=when,
            # Explicit for the same reason as above: the model defaults to ``pending``
            # and ``skillsAdded`` counts only what is on the market.
            status="approved",
        )
    )
    db.commit()
    db.add_all(
        [
            RpAiLoginEvent(eid=ME, created_at=when),
            RpAiLoginEvent(eid=ME, created_at=when + timedelta(hours=1)),
            RpAiLoginEvent(eid=COLLEAGUE, created_at=when),
            # Two downloads by one person: two downloads, one downloader.
            RpAiSkillDownload(skill_id="s1", eid=ME, created_at=when),
            RpAiSkillDownload(skill_id="s1", eid=ME, created_at=when),
        ]
    )
    db.commit()

    body = client.get("/api/usage-report", headers=_auth(ME)).json()

    assert body["complete"] is True
    assert body["activePeople"] == 2          # signing in twice counts once
    assert body["newPeople"] == 2
    assert body["skillsAdded"] == 1
    assert body["downloads"] == 2
    assert body["distinctDownloaders"] == 1
    assert body["topDownloads"][0]["objectId"] == "s1"
    assert body["topDownloads"][0]["count"] == 2


def test_this_week_is_marked_incomplete(client):
    """So a partial week is not read as a collapse in usage."""
    body = client.get("/api/usage-report?weeksBack=0", headers=_auth(ME)).json()

    assert body["complete"] is False


def test_events_for_a_deleted_market_item_stop_counting(client, db):
    """Deleting an agent must take its opens out of the numbers, not just its row.

    ``rp_ai_usage_event`` carries no foreign key to ``rp_ai_market_item`` — on
    purpose, so a report can still name something that has since gone, and the row
    keeps ``object_name`` as it was. The side effect was that ``market.delete_item``
    hard-deleted the item, the cascade took its downloads and favourites, and the
    opens and runs went on being counted against a thing nobody could find any more.

    Both halves are asserted from one fixture: the surviving item's event counts, the
    deleted one's does not, so a filter that simply dropped every market event would
    fail here too.
    """
    last_week = current_week(weeks_back=1)
    when = last_week.since + timedelta(hours=1)
    db.add_all(
        [
            RpAiMarketItem(
                id="still-here", name="Still Here", item_type="agent",
                service_type="cloud", created_at=when, status="approved",
            ),
            RpAiMarketItem(
                id="deleted-later", name="Deleted Later", item_type="agent",
                service_type="cloud", created_at=when, status="approved",
            ),
        ]
    )
    db.commit()
    db.add_all(
        [
            RpAiUsageEvent(
                eid=ME, verb="tool_open", object_type="market_item",
                object_id="still-here", object_name="Still Here", created_at=when,
            ),
            RpAiUsageEvent(
                eid=ME, verb="tool_open", object_type="market_item",
                object_id="deleted-later", object_name="Deleted Later", created_at=when,
            ),
            # A built-in portal tool page: object_type 'tool', and it has no row in
            # rp_ai_market_item and never had one. It must keep counting, which is why
            # the filter checks the object type before requiring existence.
            RpAiUsageEvent(
                eid=ME, verb="tool_run", object_type="tool",
                object_id="product-structure", object_name="Product Structure",
                created_at=when,
            ),
        ]
    )
    db.commit()

    before = client.get(f"/api/users/{ME}/tools?days=30", headers=_auth(ME)).json()
    assert {t["objectId"] for t in before} == {
        "still-here",
        "deleted-later",
        "product-structure",
    }

    db.delete(db.get(RpAiMarketItem, "deleted-later"))
    db.commit()

    after = client.get(f"/api/users/{ME}/tools?days=30", headers=_auth(ME)).json()
    assert {t["objectId"] for t in after} == {"still-here", "product-structure"}


def test_an_asset_nobody_downloaded_is_reported_as_unused(client, db):
    last_week = current_week(weeks_back=1)
    when = last_week.since + timedelta(hours=1)
    db.add_all(
        [
            RpAiSkill(
                id="used", name="Used", category_id="public", created_at=when,
                status="approved",
            ),
            RpAiSkill(
                id="ignored", name="Ignored", category_id="public", created_at=when,
                status="approved",
            ),
        ]
    )
    db.commit()
    db.add(RpAiSkillDownload(skill_id="used", eid=ME, created_at=when))
    db.commit()

    body = client.get("/api/usage-report", headers=_auth(ME)).json()

    assert body["unusedAssets"] == 1


def test_blocked_uploads_are_reported_with_their_reasons(client, db):
    last_week = current_week(weeks_back=1)
    when = last_week.since + timedelta(hours=1)
    db.add_all(
        [
            RpAiSkillAuditRecord(
                id="a1", state="danger", reason="hardcoded credential", created_at=when,
                updated_at=when,
            ),
            RpAiSkillAuditRecord(
                id="a2", state="danger", reason="hardcoded credential", created_at=when,
                updated_at=when,
            ),
            RpAiSkillAuditRecord(
                id="a3", state="pass", created_at=when, updated_at=when,
            ),
        ]
    )
    db.commit()

    body = client.get("/api/usage-report", headers=_auth(ME)).json()

    assert body["auditsTotal"] == 3
    assert body["auditsBlocked"] == 2
    assert body["auditReasons"] == [{"label": "hardcoded credential", "count": 2}]


def test_group_activity_uses_the_snapshot_not_todays_department(client, db):
    """A move between teams must not rewrite a past week's group figures."""
    last_week = current_week(weeks_back=1)
    when = last_week.since + timedelta(hours=1)
    db.add(RpAiSkill(id="s1", name="Skill One", category_id="public", created_at=when))
    db.add(RpAiPortal(eid=ME, department="SOMEWHERE ELSE ENTIRELY"))
    db.commit()
    db.add(
        RpAiSkillDownload(
            skill_id="s1", eid=ME, department="BNEW RTE RP PS FS", created_at=when
        )
    )
    db.commit()

    body = client.get("/api/usage-report", headers=_auth(ME)).json()

    labels = {g["label"] for g in body["byGroup"]}
    assert "product-system" in labels, body["byGroup"]
