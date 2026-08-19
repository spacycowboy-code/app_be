"""Category counts obey the same visibility rule as the skill list.

The frontend uses "this group's count is zero" to decide that opening the market
filtered to someone's own group would show an empty page. If the count included
skills the list then withholds — one awaiting review, say — that decision would
send people to a blank market and look like a loading bug.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.category import RpAiCategory
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal

OWNER = "EOWNSIT"
STRANGER = "ESTRANG"
GROUP = "count-probe-group"


@pytest.fixture()
def client():
    with SessionLocal() as db:
        if db.get(RpAiCategory, GROUP) is None:
            db.add(RpAiCategory(id=GROUP, label="Count Probe", count=0))
        if db.get(RpAiCategory, "all") is None:
            db.add(RpAiCategory(id="all", label="All", count=0))
        # rp_ai_skill.uploader_eid is a foreign key and the suite enforces sqlite FKs.
        if db.query(RpAiPortal).filter(RpAiPortal.eid == OWNER).one_or_none() is None:
            db.add(RpAiPortal(eid=OWNER, display_name="Owner"))
        db.commit()
    with SessionLocal() as db:
        db.add(
            RpAiSkill(
                id="count-probe-pending",
                name="Count Probe Pending",
                category_id=GROUP,
                status="pending",
                uploader_eid=OWNER,
                version="1.0.0",
                downloads="0",
                rating=0.0,
                review_count=0,
                tags=[],
            )
        )
        db.commit()
    yield TestClient(app)
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, "count-probe-pending")
        if skill:
            db.delete(skill)
        cat = db.get(RpAiCategory, GROUP)
        if cat:
            db.delete(cat)
        db.commit()


def _count(client, eid: str | None) -> int:
    headers = {"X-User-Eid": eid} if eid else {}
    body = client.get("/api/categories", headers=headers).json()
    return next(c["count"] for c in body if c["id"] == GROUP)


def _listed(client, eid: str | None) -> int:
    headers = {"X-User-Eid": eid} if eid else {}
    body = client.get("/api/skills", params={"categoryId": GROUP}, headers=headers).json()
    return len(body)


def test_a_skill_awaiting_review_is_counted_only_for_those_who_can_see_it(client):
    assert _count(client, STRANGER) == 0
    assert _count(client, OWNER) == 1
    assert _count(client, get_settings().audit_admin_eid) == 1


def test_the_count_matches_what_the_list_returns(client):
    """The two are read together; disagreeing is what breaks the empty-group rule."""
    for eid in (STRANGER, OWNER, get_settings().audit_admin_eid):
        assert _count(client, eid) == _listed(client, eid), f"disagreed for {eid}"


def test_all_counts_exactly_what_the_unfiltered_list_returns(client):
    """Compared against the list, not against the sum of the other chips: a skill
    whose category has no row would be missing from that sum but present here."""
    body = client.get("/api/categories", headers={"X-User-Eid": STRANGER}).json()
    total = next(c["count"] for c in body if c["id"] == "all")
    listed = client.get("/api/skills", headers={"X-User-Eid": STRANGER}).json()
    assert total == len(listed)
