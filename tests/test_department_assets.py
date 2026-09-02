"""The department pages: who is listed, who is counted, and what falls through.

Three things here are load-bearing rather than incidental:

* attribution is by ``category_id`` (the group the uploader filed it under), so a
  page and the market's own group filter cannot disagree;
* anything the nine departments do not claim has to land on the unassigned page.
  More than half of production sits there today — the official imports, the
  GitLab-synced items with no creator, and the ``public``/``ai-team`` groups — so
  a bug that silently drops them would hide most of the library;
* the review gate applies here too. This is a second read path onto the same
  rows, and "not listed on the market" is worth nothing if a department page
  lists it.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.services.departments import DEPARTMENTS, UNASSIGNED_SLUG

OWNER = "EDEPTOW"
STRANGER = "EDEPTST"

# Every row this module creates, so teardown cannot leave one behind and skew a
# later test's counts.
SKILL_IDS = ("dept-rsw-skill", "dept-rsw-pending-skill", "dept-public-skill")
ITEM_IDS = (
    "dept-rsw-agent",
    "dept-rsw-tool",
    "dept-rsw-platform",
    "dept-nogroup-agent",
    "dept-official-agent",
)


def _item(item_id: str, kind: str, category_id: str | None, status: str = "approved"):
    return RpAiMarketItem(
        id=item_id,
        name=item_id,
        description="probe",
        item_type=kind,
        service_type="code",
        source="manual",
        category_id=category_id,
        tags=[],
        creator=OWNER,
        status=status,
    )


def _skill(skill_id: str, category_id: str | None, status: str = "approved"):
    return RpAiSkill(
        id=skill_id,
        name=skill_id,
        category_id=category_id,
        status=status,
        uploader_eid=OWNER,
        version="1.0.0",
        downloads="0",
        rating=0.0,
        review_count=0,
        tags=[],
    )


@pytest.fixture()
def client():
    with SessionLocal() as db:
        # Created if absent, and deliberately NOT removed in teardown: these are
        # real production categories, and another module may be relying on them
        # existing. Deleting a row this fixture did not create is how one test
        # file starts breaking another.
        for slug in ("radio-sw", "public", "official"):
            if db.get(RpAiCategory, slug) is None:
                db.add(RpAiCategory(id=slug, label=slug, count=0))
        if db.query(RpAiPortal).filter(RpAiPortal.eid == OWNER).one_or_none() is None:
            db.add(RpAiPortal(eid=OWNER, display_name="Dept Owner"))
        db.commit()
    with SessionLocal() as db:
        db.add_all(
            [
                _skill("dept-rsw-skill", "radio-sw"),
                _skill("dept-rsw-pending-skill", "radio-sw", status="pending"),
                _skill("dept-public-skill", "public"),
                _item("dept-rsw-agent", "agent", "radio-sw"),
                _item("dept-rsw-tool", "tool", "radio-sw"),
                _item("dept-rsw-platform", "ai-platform", "radio-sw"),
                _item("dept-nogroup-agent", "agent", None),
                _item("dept-official-agent", "agent", "official"),
            ]
        )
        db.commit()
    yield TestClient(app)
    with SessionLocal() as db:
        for skill_id in SKILL_IDS:
            row = db.get(RpAiSkill, skill_id)
            if row:
                db.delete(row)
        for item_id in ITEM_IDS:
            row = db.get(RpAiMarketItem, item_id)
            if row:
                db.delete(row)
        db.commit()


def _assets(client, slug: str, eid: str | None = STRANGER) -> dict:
    headers = {"X-User-Eid": eid} if eid else {}
    response = client.get(f"/api/departments/{slug}/assets", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _names(assets: dict, section: str) -> set[str]:
    return {row["name"] for row in assets[section]}


def test_the_sidebar_lists_the_nine_departments_then_the_unassigned_bucket(client):
    body = client.get("/api/departments", headers={"X-User-Eid": STRANGER}).json()
    assert [d["slug"] for d in body] == [d.slug for d in DEPARTMENTS] + [UNASSIGNED_SLUG]
    assert [d["code"] for d in body][:9] == [
        "PS",
        "IVC",
        "RD1",
        "RD2",
        "RD3",
        "RSW",
        "QA",
        "PMO",
        "CD",
    ]


def test_an_item_is_listed_under_the_group_it_was_filed_in(client):
    assets = _assets(client, "radio-sw")
    assert "dept-rsw-agent" in _names(assets, "agents")
    assert "dept-rsw-tool" in _names(assets, "tools")
    assert "dept-rsw-skill" in _names(assets, "skills")


def test_the_headline_numbers_count_exactly_the_cards_below_them(client):
    """The metrics and the lists are read together; disagreeing is the bug.

    Asserted against the response's own lists rather than fixed numbers, so the
    property still holds when the suite's other tests add rows.
    """
    for slug in ("radio-sw", UNASSIGNED_SLUG):
        assets = _assets(client, slug)
        metrics = assets["metrics"]
        assert metrics["skillCount"] == len(assets["skills"]), slug
        assert metrics["agentCount"] == len(assets["agents"]), slug
        assert metrics["toolCount"] == len(assets["tools"]), slug


def test_a_group_outside_the_nine_lands_on_the_unassigned_page(client):
    """``public``/``official`` are real category rows that no department owns."""
    unassigned = _names(_assets(client, UNASSIGNED_SLUG), "agents")
    assert "dept-official-agent" in unassigned
    for dept in DEPARTMENTS:
        assert "dept-official-agent" not in _names(_assets(client, dept.slug), "agents")


def test_an_item_with_no_group_at_all_lands_on_the_unassigned_page(client):
    """NULL is not caught by ``NOT IN``, which is how these go missing."""
    assert "dept-nogroup-agent" in _names(_assets(client, UNASSIGNED_SLUG), "agents")


def test_a_skill_awaiting_review_is_hidden_from_everyone_but_its_uploader(client):
    assert "dept-rsw-pending-skill" not in _names(_assets(client, "radio-sw"), "skills")
    assert "dept-rsw-pending-skill" in _names(_assets(client, "radio-sw", OWNER), "skills")
    admin = get_settings().audit_admin_eid
    assert "dept-rsw-pending-skill" in _names(_assets(client, "radio-sw", admin), "skills")


def test_ai_platform_items_are_neither_listed_nor_counted(client):
    """The page renders three kinds; a fourth in the metrics would not add up."""
    assets = _assets(client, "radio-sw")
    every_name = _names(assets, "skills") | _names(assets, "agents") | _names(assets, "tools")
    assert "dept-rsw-platform" not in every_name


def test_a_department_with_no_category_row_answers_with_zero_rather_than_404(client):
    """``cd`` has no category row yet. The page must still exist and say so."""
    assets = _assets(client, "cd")
    assert assets["metrics"] == {"skillCount": 0, "agentCount": 0, "toolCount": 0}
    assert assets["skills"] == assets["agents"] == assets["tools"] == []


def test_the_sidebar_counts_agree_with_each_department_page(client):
    """The list endpoint counts with grouped SQL; the page counts rows it loaded.

    Two different queries answering the same question is exactly where a sidebar
    badge starts disagreeing with the page it links to, so pin them together.
    """
    body = client.get("/api/departments", headers={"X-User-Eid": STRANGER}).json()
    for summary in body:
        page = _assets(client, summary["slug"])
        assert summary["metrics"] == page["metrics"], summary["slug"]


def test_an_unknown_department_is_a_404(client):
    response = client.get("/api/departments/not-a-department/assets")
    assert response.status_code == 404
