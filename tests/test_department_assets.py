"""The department pages: who is listed, who is counted, and what falls through.

Three things here are load-bearing rather than incidental:

* attribution is by the **uploader's LDAP organisation**, not by ``category_id``.
  These pages and the market's group filter are therefore *expected* to disagree
  about the same asset — the market answers "what function is this for?" and
  these pages answer "which organisation produced it?". Several tests below
  assert the disagreement directly, because it is the feature and a later reader
  would otherwise be tempted to "fix" it;
* anything that resolves to no RP department has to land on the unassigned page.
  A fifth of production sits there today — owners whose directory row carries no
  department, owners outside RP, and rows with no owner at all — so a bug that
  silently drops them would hide a large slice of the library, and it would hide
  it in the one view whose job is to account for it;
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

#: Radio SW by organisation (``SWD`` is the fourth code).
OWNER = "EDEPTOW"
#: Signed in, but the directory gave us no department — the single largest reason
#: a real asset is unattributable today.
NO_DEPT = "EDEPTNOD"
#: A real employee outside Radio Products entirely. No page represents them.
OUTSIDE = "EDEPTRA"
#: Outside RP, but their fourth org code COLLIDES with a mapped branch code.
#: Without the ``BNEW RTE RP`` check this person's assets would be filed under
#: Radio Products' QA page — a different organisation with the same short code.
COLLIDING = "EDEPTCOL"
#: Chengdu. Their work belongs on the CD page while the market files it by
#: function, which is the whole point of the split.
CHENGDU = "EDEPTCD"
#: Used only as an ``author`` on a row with no ``creator``.
AUTHOR_ONLY = "EDEPTAUT"

STRANGER = "EDEPTST"

PEOPLE = {
    OWNER: "BNEW RTE RP SWD SW1 Software 1",
    NO_DEPT: None,
    OUTSIDE: "BNEW RTE RA RD5 Radio Design 5",
    COLLIDING: "BNEW RTE TSM QA Quality Assurance",
    CHENGDU: "BNEW RTE RP CD IVC T1 Team 1",
    AUTHOR_ONLY: "BNEW RTE RP QA Quality Assurance",
}

# Every row this module creates, so teardown cannot leave one behind and skew a
# later test's counts.
SKILL_IDS = ("dept-rsw-skill", "dept-rsw-pending-skill", "dept-misfiled-skill")
ITEM_IDS = (
    "dept-rsw-agent",
    "dept-rsw-tool",
    "dept-rsw-platform",
    "dept-noowner-agent",
    "dept-outside-agent",
    "dept-colliding-agent",
    "dept-cd-agent",
    "dept-author-agent",
)


def _item(
    item_id: str,
    kind: str,
    category_id: str | None,
    status: str = "approved",
    creator: str | None = OWNER,
    author: str | None = None,
):
    return RpAiMarketItem(
        id=item_id,
        name=item_id,
        description="probe",
        item_type=kind,
        service_type="code",
        source="manual",
        category_id=category_id,
        tags=[],
        creator=creator,
        author=author,
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
        for slug in ("radio-sw", "public", "official", "ivc"):
            if db.get(RpAiCategory, slug) is None:
                db.add(RpAiCategory(id=slug, label=slug, count=0))
        # The department each probe row is attributed through. Set explicitly
        # every time rather than only on insert, because attribution now reads
        # this column and a row another module left behind with a stale value
        # would move these assets to a different page.
        for eid, department in PEOPLE.items():
            row = db.query(RpAiPortal).filter(RpAiPortal.eid == eid).one_or_none()
            if row is None:
                row = RpAiPortal(eid=eid, display_name=f"Probe {eid}")
                db.add(row)
            row.department = department
        db.commit()
    with SessionLocal() as db:
        db.add_all(
            [
                _skill("dept-rsw-skill", "radio-sw"),
                _skill("dept-rsw-pending-skill", "radio-sw", status="pending"),
                # Filed under a group its uploader does not belong to. Under the
                # old basis this landed on the ``public`` side of the split; now
                # the category is not consulted at all.
                _skill("dept-misfiled-skill", "public"),
                _item("dept-rsw-agent", "agent", "radio-sw"),
                _item("dept-rsw-tool", "tool", "radio-sw"),
                _item("dept-rsw-platform", "ai-platform", "radio-sw"),
                _item("dept-noowner-agent", "agent", None, creator=None),
                _item("dept-outside-agent", "agent", "radio-sw", creator=OUTSIDE),
                _item("dept-colliding-agent", "agent", "radio-sw", creator=COLLIDING),
                # Chengdu's own work, filed under the FUNCTION it serves.
                _item("dept-cd-agent", "agent", "ivc", creator=CHENGDU),
                # No creator, but the author field holds a real signum.
                _item("dept-author-agent", "agent", None, creator=None, author=AUTHOR_ONLY),
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


def test_an_asset_is_listed_under_its_owners_organisation(client):
    assets = _assets(client, "radio-sw")
    assert "dept-rsw-agent" in _names(assets, "agents")
    assert "dept-rsw-tool" in _names(assets, "tools")
    assert "dept-rsw-skill" in _names(assets, "skills")


def test_the_market_group_is_not_consulted(client):
    """A skill filed under ``public`` still counts towards its uploader's page.

    This is the sharpest statement of the change: the row's ``category_id`` says
    ``public``, which no department owns, and under the old basis that put it on
    the unassigned page. Its uploader is in ``SWD``, so it now belongs to Radio
    SW — and nothing about the row itself had to change.

    Asserted from both ends, because only checking that it appears under Radio SW
    would also pass if the unassigned page had started listing everything.
    """
    assert "dept-misfiled-skill" in _names(_assets(client, "radio-sw"), "skills")
    assert "dept-misfiled-skill" not in _names(_assets(client, UNASSIGNED_SLUG), "skills")


def test_chengdus_work_is_on_chengdus_page_while_the_market_files_it_by_function(client):
    """The two views disagree about one asset, on purpose.

    ``dept-cd-agent`` carries ``category_id='ivc'`` because verification tooling
    is verification tooling on either site, so the Skill/Agent market shows it
    under ``IVC`` beside Beijing's. Its creator is in ``BNEW RTE RP CD``, so this
    page shows it under Chengdu. Both are correct answers to different questions.

    Kept as an explicit test because the disagreement looks like a bug to anyone
    who meets it without the context, and the obvious "fix" — making one of the
    two follow the other — silently destroys one of the two answers.
    """
    assert "dept-cd-agent" in _names(_assets(client, "cd"), "agents")
    assert "dept-cd-agent" not in _names(_assets(client, "ivc"), "agents")


def test_the_headline_numbers_count_exactly_the_cards_below_them(client):
    """The metrics and the lists are read together; disagreeing is the bug.

    Asserted against the response's own lists rather than fixed numbers, so the
    property still holds when the suite's other tests add rows.
    """
    for slug in ("radio-sw", "cd", UNASSIGNED_SLUG):
        assets = _assets(client, slug)
        metrics = assets["metrics"]
        assert metrics["skillCount"] == len(assets["skills"]), slug
        assert metrics["agentCount"] == len(assets["agents"]), slug
        assert metrics["toolCount"] == len(assets["tools"]), slug


def test_an_owner_whose_directory_row_has_no_department_lands_on_unassigned(client):
    """The largest real reason an asset is unattributable.

    In production this is 17 rows, most of them IVC skills whose owner rows were
    created by a sync that never wrote ``department``. They are not anybody's
    fault and they are not deletable, so the page has to hold them.
    """
    with SessionLocal() as db:
        db.add(
            _item("dept-nodept-agent", "agent", "radio-sw", creator=NO_DEPT)
        )
        db.commit()
    try:
        assert "dept-nodept-agent" in _names(_assets(client, UNASSIGNED_SLUG), "agents")
        assert "dept-nodept-agent" not in _names(_assets(client, "radio-sw"), "agents")
    finally:
        with SessionLocal() as db:
            row = db.get(RpAiMarketItem, "dept-nodept-agent")
            if row:
                db.delete(row)
            db.commit()


def test_an_owner_outside_rp_lands_on_the_unassigned_page(client):
    """``BNEW RTE RA RD5`` is a real department with no page, by choice.

    Inventing one would put five people's assets under a heading no reader could
    place. Note the row's ``category_id`` is ``radio-sw`` — proof that it is the
    owner's organisation being read and not the filing.
    """
    assert "dept-outside-agent" in _names(_assets(client, UNASSIGNED_SLUG), "agents")
    for dept in DEPARTMENTS:
        assert "dept-outside-agent" not in _names(_assets(client, dept.slug), "agents")


def test_a_matching_code_under_a_different_org_is_not_claimed(client):
    """``BNEW RTE TSM QA`` is not Radio Products' QA, despite the same code.

    Org codes are only unique within their parent, so the fourth segment alone is
    ambiguous — this is why the resolver checks the ``BNEW RTE RP`` prefix before
    reading it. Without that check this person's assets appear on the QA page, and
    nothing looks wrong: the code matches, the label matches, and the department
    string is never shown next to the card.

    The previous test cannot catch this, because ``RD5`` happens not to be a
    mapped branch code — it would pass with the prefix check removed.
    """
    assert "dept-colliding-agent" in _names(_assets(client, UNASSIGNED_SLUG), "agents")
    assert "dept-colliding-agent" not in _names(_assets(client, "qa"), "agents")


def test_an_item_with_no_owner_at_all_lands_on_the_unassigned_page(client):
    """Synced rows carry no ``creator``; there is nothing to attribute them by."""
    assert "dept-noowner-agent" in _names(_assets(client, UNASSIGNED_SLUG), "agents")


def test_a_signum_author_is_used_when_there_is_no_creator(client):
    """``author`` is the fallback, and it rescues three real rows in production.

    Without it they would sit on the unassigned page while the card printed their
    author's name — the page disclaiming knowledge of something it is displaying.
    """
    assert "dept-author-agent" in _names(_assets(client, "qa"), "agents")
    assert "dept-author-agent" not in _names(_assets(client, UNASSIGNED_SLUG), "agents")


def test_a_display_name_author_is_not_resolved(client):
    """Two colleagues can share a display name, so a name identifies nobody.

    Resolving it would attribute the asset to whoever happened to be found first,
    which is worse than admitting the row is unattributable.
    """
    with SessionLocal() as db:
        row = db.query(RpAiPortal).filter(RpAiPortal.eid == "EDEPTNAM").one_or_none()
        if row is None:
            row = RpAiPortal(eid="EDEPTNAM", display_name="Probe Name")
            db.add(row)
        row.department = "BNEW RTE RP QA Quality Assurance"
        db.add(_item("dept-name-agent", "agent", None, creator=None, author="Probe Name"))
        db.commit()
    try:
        assert "dept-name-agent" in _names(_assets(client, UNASSIGNED_SLUG), "agents")
        assert "dept-name-agent" not in _names(_assets(client, "qa"), "agents")
    finally:
        with SessionLocal() as db:
            row = db.get(RpAiMarketItem, "dept-name-agent")
            if row:
                db.delete(row)
            db.commit()


def test_item_owner_returns_an_eid_or_nothing() -> None:
    """``_item_owner`` promises an EID or ``None`` — pinned at the unit level.

    The endpoint cannot tell a broken version of this apart from a working one: a
    display name that leaked through would fail the EID lookup and land on the
    same page anyway. So the contract is asserted here, where returning
    ``'PROBE NAME'`` instead of ``None`` is visible.
    """
    from app.routers.departments import _item_owner

    assert _item_owner(_item("x", "agent", None, creator="ecnabcd")) == "ECNABCD"
    assert _item_owner(_item("x", "agent", None, creator=None, author="ecnabcd")) == "ECNABCD"
    assert _item_owner(_item("x", "agent", None, creator=None, author="Probe Name")) is None
    assert _item_owner(_item("x", "agent", None, creator=None, author=None)) is None
    # creator wins outright; author is only consulted when there is no creator.
    both = _item("x", "agent", None, creator="ecnaaaa", author="ecnbbbb")
    assert _item_owner(both) == "ECNAAAA"


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


def test_the_sidebar_counts_agree_with_each_department_page(client):
    """One endpoint counts every row; the other loads one department's rows.

    Both route through the same attribution function, which is the point — two
    expressions of the same rule is exactly where a sidebar badge starts
    disagreeing with the page it links to.
    """
    body = client.get("/api/departments", headers={"X-User-Eid": STRANGER}).json()
    for summary in body:
        page = _assets(client, summary["slug"])
        assert summary["metrics"] == page["metrics"], summary["slug"]


def test_an_unknown_department_is_a_404(client):
    response = client.get("/api/departments/not-a-department/assets")
    assert response.status_code == 404
