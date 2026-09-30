"""Two things the department report used to get wrong about attribution.

**The duplicate unit row.** ``aggregate`` joins two key spaces: a person ON the
Graph reporting tree is keyed by their branch code (``RD1``), a person who is not is
keyed by the code path their own ``department`` string parses to
(``BNEW/RTE/RP/RD1``). Those never collide, so one colleague missing from the tree
produced a SECOND row for a unit that already had one — in production, ``RP / RD1``
with a dash for a head count, sitting underneath the real ``RD1 | 99 | 43 | 43%``.
Nothing on the page said they were the same unit. They are now collected into a
single ``Other`` row.

They are deliberately NOT folded into the real unit, and one test below pins that:
the head count comes from the tree and excludes them, so their activity would be
divided by a denominator they are not part of.

**The contributor table said nothing about type.** It had one ``上传市场项`` column
covering agents, tools and platform entries together, and its only "被用" column was
for skills — so a person who had put three tools into the market and had them taken
read as a single number in one column, and the section titled "谁的东西被用" could
not answer that question for anything but skills. Agents and tools now have their
own upload columns and share a take-up column, which is fed by market data that was
already being computed and never rendered.
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload
from app.services.usage_report import figures
from app.services.usage_report.i18n import _EN, _ZH
from app.services.usage_report.render import (
    OTHER_BIG_KEY,
    OTHER_BIG_LABEL,
    Dept,
    Person,
    aggregate,
)

PREFIX = "other-row-probe"
AUTHOR = "EATTRIB"

#: Keyed the way the org chart keys things: bare branch code.
BIG_HEAD = {"RD1": 99, "SWD": 73}


def _dept(small: str, big: str, label: str) -> Dept:
    return Dept(small, small.replace("/", " "), big, label, "BNEW/RTE/RP")


def _person(eid: str, dept: Dept) -> Person:
    return Person(eid=eid, dept=dept, source="tree", signed_in=True)


def _groups(people: dict[str, Person], *, in_scope: bool = True):
    return aggregate(
        people, [], BIG_HEAD, {}, {}, {}, "big", in_scope=in_scope
    )


def test_someone_the_org_chart_does_not_place_no_longer_forks_their_unit():
    """One RD1 row, not two. The stray lands in Other, keeping its own row.

    The key is what makes this happen: ``BNEW/RTE/RP/RD1`` is not ``RD1``, so before
    this the two never met and the table showed the unit twice.
    """
    stray = _person("ESTRAY", _dept("BNEW/RTE/RP/RD1/ALG", "BNEW/RTE/RP/RD1", "RP / RD1"))
    groups = _groups({"ESTRAY": stray})
    assert OTHER_BIG_KEY in groups
    assert groups[OTHER_BIG_KEY].label == OTHER_BIG_LABEL
    assert groups[OTHER_BIG_KEY].users == 1
    # The seeded RD1 row is still there and still has only its own head count.
    assert groups["RD1"].users == 0
    assert groups["RD1"].real == 99


def test_several_strays_share_one_other_row():
    """However many units the directory is missing, the reader gets one row.

    A row per stray is the alternative, and it grows a new dashed near-duplicate
    every time somebody moves team.
    """
    people = {
        "EA": _person("EA", _dept("BNEW/RTE/RP/RD1/ALG", "BNEW/RTE/RP/RD1", "RP / RD1")),
        "EB": _person("EB", _dept("BNEW/RTE/RP/XYZ/T1", "BNEW/RTE/RP/XYZ", "RP / XYZ")),
    }
    groups = _groups(people)
    assert [g for g in groups if g not in BIG_HEAD] == [OTHER_BIG_KEY]
    assert groups[OTHER_BIG_KEY].users == 2


def test_the_other_row_has_no_head_count_so_it_claims_no_adoption():
    """This is the reason they are not folded into the unit they name.

    ``real`` stays 0, so the row shows a dash and 0% rather than borrowing a
    denominator that was built without them.
    """
    stray = _person("ESTRAY", _dept("BNEW/RTE/RP/RD1/ALG", "BNEW/RTE/RP/RD1", "RP / RD1"))
    other = _groups({"ESTRAY": stray})[OTHER_BIG_KEY]
    assert other.real == 0
    assert other.adoption == 0


def test_outside_the_scope_units_keep_their_own_codes():
    """Section five is a list of individuals from other organisations.

    Collapsing those into "Other" would delete the only thing that section says.
    """
    outsider = _person("EOUT", _dept("BNEW/RTE/RU/PS/T1", "BNEW/RTE/RU/PS", "RU / PS"))
    groups = _groups({"EOUT": outsider}, in_scope=False)
    assert OTHER_BIG_KEY not in groups
    assert groups["BNEW/RTE/RU/PS"].label == "RU / PS"


@pytest.mark.parametrize("strings", [_ZH, _EN])
def test_the_other_row_is_explained_in_both_languages(strings):
    """An unexplained dashed row reads as a bug to whoever is handed the page."""
    assert "lead_other_row" in strings
    assert "{people}" in strings["lead_other_row"]


# --- contributor table ------------------------------------------------------


@pytest.fixture()
def market():
    """One person with two agents, one tool, one platform entry, and take-ups."""
    with SessionLocal() as db:
        for suffix, item_type in (("a1", "agent"), ("a2", "agent"),
                                  ("t1", "tool"), ("p1", "ai-platform")):
            db.add(RpAiMarketItem(
                id=f"{PREFIX}-{suffix}", name=f"Probe {suffix}", item_type=item_type,
                service_type="code", source="upload", creator=AUTHOR,
                status="approved"))
        db.commit()
        # Same person taking the same tool twice is one take-up, as on the skill
        # side; a second person makes the two numbers differ.
        for eid, suffix in (("ETAKER1", "t1"), ("ETAKER1", "t1"),
                            ("ETAKER2", "t1"), ("ETAKER2", "a1")):
            db.add(RpAiMarketDownload(item_id=f"{PREFIX}-{suffix}", eid=eid))
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(RpAiMarketDownload).filter(
            RpAiMarketDownload.item_id.like(f"{PREFIX}%")
        ).delete(synchronize_session=False)
        db.query(RpAiMarketItem).filter(
            RpAiMarketItem.id.like(f"{PREFIX}%")
        ).delete(synchronize_session=False)
        db.commit()


def _contrib():
    with SessionLocal() as db:
        return figures.contributions(db.connection())


def test_agents_and_tools_are_counted_apart(market):
    """Two agents and one tool, not "three market items"."""
    rows, _ = _contrib()
    assert rows[AUTHOR]["agents"] == 2
    assert rows[AUTHOR]["tools"] == 1


def test_the_combined_count_is_kept_and_is_larger_than_the_two(market):
    """It is still the row's "did they put anything in the market" test.

    4 rather than 3: the platform entry is a third type and is in neither column,
    which is what the note under the table exists to say.
    """
    rows, gaps = _contrib()
    assert rows[AUTHOR]["market_items"] == 4
    assert gaps["platform_items"] >= 1


def test_market_take_ups_count_distinct_item_person_pairs(market):
    """Same rule as the skill column beside it: 3 pairs from 2 people.

    ETAKER1 took the tool twice and that is one pair, or a person re-downloading
    their colleague's tool would inflate the author's contribution.
    """
    rows, _ = _contrib()
    assert rows[AUTHOR]["market_takers"] == 2
    assert rows[AUTHOR]["market_taken"] == 3


@pytest.mark.parametrize("strings", [_ZH, _EN])
def test_the_new_contributor_columns_have_labels(strings):
    """``person_table`` takes the header strings directly; a gap is a KeyError."""
    for key in ("col_contrib_agent", "col_contrib_tool", "col_contrib_market_taken"):
        assert key in strings and strings[key].strip()


@pytest.mark.parametrize("strings", [_ZH, _EN])
def test_the_replaced_combined_column_label_is_gone(strings):
    """Left behind, it is dead text that invites someone to re-add the column."""
    assert "col_contrib_market" not in strings
