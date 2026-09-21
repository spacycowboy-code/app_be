"""The admin usage export: named individuals, and nothing silently dropped.

This file is the mirror image of ``test_usage_events.py``'s
``test_the_report_names_no_individuals``: the weekly report must not name people,
and this export must. Both properties are asserted so that widening one into the
other has to break a test rather than pass review unnoticed.

The other three things pinned here are each a row that is easy to lose:

* an **anonymous** download (``eid`` is nullable on the download tables) must be
  emitted and counted, not skipped — otherwise the file undercounts real
  downloads and nobody can tell;
* a usage event whose market item has been **deleted** must survive, flagged,
  because it carries no foreign key on purpose and the weekly report drops it;
* an asset with **no activity** must still be listed, because "which of these is
  nobody using" is one of the questions the export exists to answer.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.download import RpAiSkillDownload
from app.models.favorite import RpAiUserFavorite
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.models.market_favorite import RpAiMarketFavorite
from app.models.skill import RpAiSkill
from app.models.usage_event import RpAiUsageEvent
from app.models.user import RpAiPortal
from app.services.auth_tokens import create_token
from app.services.reporting_windows import db_now

ME = "EZHGMIO"          # a dashboard EID, per the default settings
COLLEAGUE = "EOTHER"


@pytest.fixture()
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "export-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    return TestClient(app)


@pytest.fixture()
def db():
    with SessionLocal() as session:
        yield session


def _auth(eid: str) -> dict:
    token, _ = create_token(eid)
    return {"Authorization": f"Bearer {token}"}


def _fixture(db) -> None:
    """One skill and one agent, each downloaded and favourited, plus tool clicks."""
    now = db_now()
    db.add_all(
        [
            RpAiPortal(eid=ME, display_name="Miao Zhang M", department="BNEW RTE RP PS AI"),
            RpAiPortal(eid=COLLEAGUE, display_name="Other Person", department="BNEW RTE RP RD1"),
            RpAiSkill(id="s1", name="Skill One", category_id="public", version="1.2.0",
                      status="approved"),
            RpAiSkill(id="s-idle", name="Nobody Wants This", category_id="public",
                      status="approved"),
            RpAiMarketItem(id="gl-9", name="Agent Nine", item_type="agent",
                           service_type="code", status="approved"),
        ]
    )
    db.commit()
    db.add_all(
        [
            # Two downloads by one person plus one by another: 3 events, 2 unique.
            RpAiSkillDownload(skill_id="s1", eid=ME, department="BNEW RTE RP PS AI",
                              created_at=now - timedelta(hours=3)),
            RpAiSkillDownload(skill_id="s1", eid=ME, department="BNEW RTE RP PS AI",
                              created_at=now - timedelta(hours=2)),
            RpAiSkillDownload(skill_id="s1", eid=COLLEAGUE, department="BNEW RTE RP RD1",
                              created_at=now - timedelta(hours=1)),
            RpAiMarketDownload(item_id="gl-9", eid=COLLEAGUE, created_at=now - timedelta(hours=1)),
            RpAiUserFavorite(eid=ME, skill_id="s1"),
            RpAiMarketFavorite(eid=COLLEAGUE, item_id="gl-9"),
            RpAiUsageEvent(eid=ME, verb="tool_open", object_type="tool",
                           object_id="product-structure", object_name="Product Structure",
                           source="tool-page", created_at=now - timedelta(hours=2)),
            RpAiUsageEvent(eid=COLLEAGUE, verb="tool_open", object_type="tool",
                           object_id="product-structure", object_name="Product Structure",
                           source="home", created_at=now - timedelta(hours=1)),
            RpAiUsageEvent(eid=ME, verb="tool_run", object_type="tool",
                           object_id="product-structure", object_name="Product Structure",
                           outcome="error", duration_ms=900, created_at=now),
        ]
    )
    db.commit()


def _get(client, query: str = "") -> dict:
    resp = client.get(f"/api/usage-export{query}", headers=_auth(ME))
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- Who may pull it --------------------------------------------------------


def test_a_colleague_cannot_pull_the_export(client):
    """Same gate as the weekly report: it names individuals."""
    assert client.get("/api/usage-export", headers=_auth(COLLEAGUE)).status_code == 403


def test_an_anonymous_caller_cannot_pull_the_export(client):
    assert client.get("/api/usage-export").status_code == 401


# --- Content ----------------------------------------------------------------


def test_it_names_the_people_behind_every_download_and_favorite(client, db):
    _fixture(db)

    body = _get(client)

    skill = next(s for s in body["skills"] if s["id"] == "s1")
    assert skill["downloadEvents"] == 3
    assert skill["uniqueDownloaders"] == 2       # ME twice counts once
    assert [d["eid"] for d in skill["downloads"]] == [ME, ME, COLLEAGUE]
    assert skill["downloads"][0]["displayName"] == "Miao Zhang M"
    # Every download row carries a time, or the export answers only half the question.
    assert all(d["at"] for d in skill["downloads"])
    assert skill["favorites"] == 1
    assert skill["favoritedBy"][0]["eid"] == ME
    assert skill["favoritedBy"][0]["at"]

    agent = next(i for i in body["marketItems"] if i["id"] == "gl-9")
    assert agent["assetKind"] == "agent"
    assert agent["uniqueDownloaders"] == 1
    assert agent["favoritedBy"][0]["eid"] == COLLEAGUE


def test_tool_clicks_are_counted_per_person_with_their_times(client, db):
    _fixture(db)

    tool = next(t for t in _get(client)["tools"] if t["objectId"] == "product-structure")

    assert tool["opens"] == 2                    # the click count
    assert tool["runs"] == 1                     # never summed with opens
    assert tool["failedRuns"] == 1
    assert tool["uniqueUsers"] == 2
    assert [(e["eid"], e["verb"]) for e in tool["events"]] == [
        (ME, "tool_open"),
        (COLLEAGUE, "tool_open"),
        (ME, "tool_run"),
    ]
    assert all(e["at"] for e in tool["events"])
    assert tool["events"][0]["source"] == "tool-page"


def test_an_asset_nobody_touched_is_still_listed(client, db):
    """"Which of these is unused" is one of the questions this file answers."""
    _fixture(db)

    idle = next(s for s in _get(client)["skills"] if s["id"] == "s-idle")

    assert idle["downloadEvents"] == 0
    assert idle["uniqueDownloaders"] == 0
    assert idle["downloads"] == []


def test_an_anonymous_download_is_counted_not_dropped(client, db):
    """``eid`` is nullable on the download tables — those are still downloads."""
    db.add(RpAiSkill(id="s1", name="Skill One", category_id="public", status="approved"))
    db.commit()
    db.add(RpAiSkillDownload(skill_id="s1", eid=None))
    db.commit()

    skill = next(s for s in _get(client)["skills"] if s["id"] == "s1")

    assert skill["downloadEvents"] == 1
    assert skill["anonymousDownloads"] == 1
    assert skill["uniqueDownloaders"] == 0       # nobody to attribute it to
    assert skill["downloads"][0]["eid"] is None


def test_events_for_a_deleted_item_are_kept_but_flagged(client, db):
    """They carry no FK on purpose; the weekly report drops them, this does not."""
    now = db_now()
    db.add(RpAiMarketItem(id="gone", name="Gone", item_type="agent",
                          service_type="cloud", status="approved"))
    db.commit()
    db.add(RpAiUsageEvent(eid=ME, verb="tool_open", object_type="market_item",
                          object_id="gone", object_name="Gone", created_at=now))
    db.commit()
    db.delete(db.get(RpAiMarketItem, "gone"))
    db.commit()

    tool = next(t for t in _get(client)["tools"] if t["objectId"] == "gone")

    assert tool["opens"] == 1
    assert tool["objectStillListed"] is False


def test_the_window_bounds_events_but_not_the_asset_list(client, db):
    _fixture(db)
    db.add(RpAiSkillDownload(skill_id="s1", eid=ME, created_at=db_now() - timedelta(days=90)))
    db.commit()

    body = _get(client, "?days=7")

    skill = next(s for s in body["skills"] if s["id"] == "s1")
    assert body["windowDays"] == 7
    assert skill["downloadEvents"] == 3          # the 90-day-old row is outside
    assert any(s["id"] == "s-idle" for s in body["skills"])


def test_it_is_served_as_a_dated_file(client, db):
    """Two exports a week apart are different documents; one filename cannot serve both."""
    _fixture(db)

    resp = client.get("/api/usage-export", headers=_auth(ME))

    disposition = resp.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert "usage-export-" in disposition and disposition.endswith('.json"')


def test_the_weekly_report_still_names_nobody(client, db):
    """The gate this export widens is the report's, not its no-individuals rule."""
    _fixture(db)

    raw = client.get("/api/usage-report", headers=_auth(ME)).text

    assert ME not in raw
    assert COLLEAGUE not in raw
