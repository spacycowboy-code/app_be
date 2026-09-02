"""The group reaches the cards — both markets, list and detail.

Separate from ``test_card_group.py``, which pins the label rule. This file pins
the *contract*: that the two market endpoints actually carry it, in camelCase,
and that an item with nobody to place omits it rather than inventing a site.
Without this, the rule could be perfect and every card still blank — which is
how the report's upload columns managed to lose a whole dimension while 29
tests passed.

Asymmetry worth knowing: the skill market has a single-skill endpoint and the
agent market does not — ``/api/market`` is the only reader of a market item, so
the list is the whole contract there.
"""
import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models.market import RpAiMarketItem
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal

BJ_USER = "ECARDBJ"
BJ_DEPT = "BNEW RTE RP IVC TE Test Environment"
CD_USER = "ECARDCD"
CD_DEPT = "BNEW RTE RP CD PPD Power PCB Digital"
# Known to the portal but sitting outside Radio Products: no BJ/CD answer is true.
OUTSIDE_USER = "ECARDEEE"
OUTSIDE_DEPT = "BNEW RTE EEE SP Syst & programs"

SKILL_BJ = "card-group-skill-bj"
SKILL_CD = "card-group-skill-cd"
SKILL_OUTSIDE = "card-group-skill-outside"
SKILL_NO_UPLOADER = "card-group-skill-imported"
ITEM_BJ = "card-group-item-bj"
ITEM_SYNCED = "card-group-item-synced"


@pytest.fixture()
def client():
    with SessionLocal() as db:
        for eid, dept in (
            (BJ_USER, BJ_DEPT),
            (CD_USER, CD_DEPT),
            (OUTSIDE_USER, OUTSIDE_DEPT),
        ):
            row = db.query(RpAiPortal).filter(RpAiPortal.eid == eid).one_or_none()
            if row is None:
                db.add(RpAiPortal(eid=eid, display_name=eid, department=dept))
            else:
                row.department = dept
        # Committed before the skills: ``rp_ai_skill.uploader_eid`` is a real
        # foreign key, and the unit of work does not promise to INSERT the
        # parents first.
        db.commit()
        for sid, uploader, author in (
            (SKILL_BJ, BJ_USER, BJ_USER),
            (SKILL_CD, CD_USER, CD_USER),
            (SKILL_OUTSIDE, OUTSIDE_USER, OUTSIDE_USER),
            # An official import: a label for an author, no person behind it.
            (SKILL_NO_UPLOADER, None, "Anthropic"),
        ):
            if db.get(RpAiSkill, sid) is None:
                db.add(
                    RpAiSkill(
                        id=sid,
                        name=sid,
                        status="approved",
                        uploader_eid=uploader,
                        author=author,
                        tags=[],
                    )
                )
        for iid, creator, source in (
            (ITEM_BJ, BJ_USER, "upload"),
            # Pushed in by the GitLab subgroup sync: creator is NULL by design.
            (ITEM_SYNCED, None, "gitlab-agents"),
        ):
            if db.get(RpAiMarketItem, iid) is None:
                db.add(
                    RpAiMarketItem(
                        id=iid,
                        name=iid,
                        item_type="agent",
                        service_type="code",
                        source=source,
                        status="approved",
                        creator=creator,
                        tags=[],
                    )
                )
        db.commit()
    # No teardown: conftest's autouse ``_fresh_db`` drops every table after each
    # test. Deleting these rows here as well left a second session's transaction
    # open against the sqlite file, and the ``drop_all`` that followed it did not
    # take — so the *next* test's category seed hit a UNIQUE violation. The rows
    # are hermetic already; cleaning them twice is what broke it.
    return TestClient(app)


def _by_id(payload: list[dict]) -> dict[str, dict]:
    return {row["id"]: row for row in payload}


def test_skill_market_list_carries_the_three_parts(client):
    rows = _by_id(client.get("/api/skills").json())

    bj = rows[SKILL_BJ]["group"]
    assert bj == {
        "site": "BJ",
        "bigDept": "IVC",
        "smallDept": "TE",
        "label": "BJ IVC TE",
        "department": BJ_DEPT,
    }

    # A flat Chengdu unit: two parts, and the third is null rather than a filler
    # string the template would have to recognise.
    cd = rows[SKILL_CD]["group"]
    assert (cd["site"], cd["bigDept"], cd["smallDept"]) == ("CD", "PPD", None)
    assert cd["label"] == "CD PPD"


def test_skill_detail_carries_the_same_group_as_the_card(client):
    detail = client.get(f"/api/skills/{SKILL_BJ}").json()
    assert detail["group"]["label"] == "BJ IVC TE"


@pytest.mark.parametrize("skill_id", [SKILL_OUTSIDE, SKILL_NO_UPLOADER])
def test_an_unplaceable_skill_shows_no_group(client, skill_id):
    """No group line beats a wrong one: nobody is asserted into an office."""
    assert _by_id(client.get("/api/skills").json())[skill_id]["group"] is None


def test_agent_market_list_carries_the_group(client):
    rows = _by_id(client.get("/api/market").json())
    assert rows[ITEM_BJ]["group"]["label"] == "BJ IVC TE"


def test_a_gitlab_synced_item_has_no_creator_and_so_no_group(client):
    rows = _by_id(client.get("/api/market").json())
    assert rows[ITEM_SYNCED]["creator"] is None
    assert rows[ITEM_SYNCED]["group"] is None
