"""Which assets the department report counts as a contribution.

The report had no status filter at all: every row in ``rp_ai_skill`` and
``rp_ai_market_item`` counted, whatever had become of it. Then it counted
``approved`` plus ``archived``, on the reasoning that a taken-down skill was live
and downloadable first. It now counts ``approved`` only: a deleted skill, agent or
tool must not appear in any reported number.

``skills.delete_skill`` is still a soft delete — it sets ``archived`` and keeps the
row, because the downloads, favourites, comments and audit records hanging off it
cannot be recomputed from the repository. Keeping the row is right; counting it as
a live asset is not, because what a reader sees on the portal is the ``approved``
list and an asset count larger than that list cannot be checked against anything.

A skill still in review, or one whose review was rejected, does not count either:
the public list filters on ``approved``, so neither was ever visible to anyone.
That would turn "uploaded" into "attempted to upload" — and for rejected
submissions it is a number someone can drive, since ten packages that all failed
review would read as ten uploads.

One asymmetry survives on purpose, pinned by the last test here: the
``skill_download`` column counts a person's own downloads with no status filter at
all. A download happened whatever later became of the thing downloaded, and
retracting it because the author took the skill down would report the author's
decision as the downloader's activity — and would change a number in an
already-issued monthly report.

These tests pin the rule, its consequences for the contribution table, and the two
prose figures that have to move together with the update column so the sentence
explaining it cannot contradict the table beneath it.
"""
import pytest

from app.database import SessionLocal
from app.models.download import RpAiSkillDownload
from app.models.market import RpAiMarketItem
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.models.version import RpAiSkillVersion
from app.services.usage_report import figures

AUTHOR = "ECOUNTS"
PREFIX = "counted-status-probe"


def _skill(db, suffix: str, status: str) -> str:
    """One skill by AUTHOR in the given lifecycle state, with two versions.

    Two version rows so the skill contributes exactly one *update*: the first row
    is the 1.0.0 written at upload time, which the update column excludes.
    """
    sid = f"{PREFIX}-{suffix}"
    db.add(
        RpAiSkill(
            id=sid,
            name=f"Counted Status Probe {suffix}",
            uploader_eid=AUTHOR,
            author=AUTHOR,
            status=status,
            tags=[],
        )
    )
    db.flush()
    for v in ("1.0.0", "1.1.0"):
        db.add(RpAiSkillVersion(skill_id=sid, version=v, uploader_eid=AUTHOR))
    return sid


@pytest.fixture()
def seeded():
    """One skill in each state, one market item in each, and a download each.

    Torn down explicitly rather than by rolling back: the report reads through its
    own connection, so the rows have to be committed to be visible to it.
    """
    with SessionLocal() as db:
        if db.get(RpAiPortal, AUTHOR) is None:
            db.add(RpAiPortal(eid=AUTHOR, display_name="Counts Probe"))
            db.commit()
    with SessionLocal() as db:
        for suffix, status in (
            ("live", "approved"),
            ("gone", "archived"),
            ("waiting", "pending"),
            ("refused", "rejected"),
        ):
            sid = _skill(db, suffix, status)
            db.add(RpAiSkillDownload(skill_id=sid, eid="ETAKERX"))
            db.add(
                RpAiMarketItem(
                    id=f"{PREFIX}-item-{suffix}",
                    name=f"Probe Item {suffix}",
                    item_type="agent",
                    service_type="cloud",
                    creator=AUTHOR,
                    status="approved" if status in ("approved", "archived") else status,
                )
            )
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(RpAiSkillDownload).filter(
            RpAiSkillDownload.skill_id.like(f"{PREFIX}%")
        ).delete(synchronize_session=False)
        db.query(RpAiSkillVersion).filter(
            RpAiSkillVersion.skill_id.like(f"{PREFIX}%")
        ).delete(synchronize_session=False)
        db.query(RpAiSkill).filter(RpAiSkill.id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )
        db.query(RpAiMarketItem).filter(RpAiMarketItem.id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )
        db.commit()


def _metric(key: str):
    with SessionLocal() as db:
        return {m.key: m for m in figures.collect(db.connection())}[key]


def _contrib():
    with SessionLocal() as db:
        return figures.contributions(db.connection())


def test_upload_column_counts_only_what_is_on_the_market(seeded):
    """Just the approved one. Taken down, in review and rejected are all excluded."""
    assert _metric("skill_upload").per_eid[AUTHOR] == 1


def test_update_column_follows_the_same_rule(seeded):
    """One update, from the one live skill. The taken-down skill's update is gone too."""
    assert _metric("skill_update").per_eid[AUTHOR] == 1


def test_market_upload_column_excludes_items_still_under_review(seeded):
    """Market has no archived state, so only the two approved items count."""
    assert _metric("agent_upload").per_eid[AUTHOR] == 2
    assert _metric("market_item_upload").per_eid[AUTHOR] == 2


def test_contribution_table_agrees_with_the_columns(seeded):
    """The contribution list is the same population, or the report contradicts itself."""
    rows, _ = _contrib()
    assert rows[AUTHOR]["skills"] == 1
    assert rows[AUTHOR]["versions"] == 1
    # Market items are unaffected: the fixture's two live items are both approved,
    # and market deletion removes the row outright rather than archiving it.
    assert rows[AUTHOR]["market_items"] == 2


def test_take_ups_of_a_taken_down_skill_stop_being_credited(seeded):
    """The author is credited for take-ups of live skills only.

    This is the contribution side — "how often was *my* skill taken" — and it moves
    with the upload column, so an author cannot keep accumulating credit for
    something that is no longer on the market. The downloader's own activity is a
    different question and is deliberately left alone; see the last test.
    """
    rows, _ = _contrib()
    assert rows[AUTHOR]["skill_taken"] == 1  # the live one only
    assert rows[AUTHOR]["skill_takers"] == 1


def test_the_prose_figures_move_with_the_update_column(seeded):
    """``real_updates`` explains the update column, so it must count the same rows.

    Left unfiltered, a rejected skill's version rows would make the report state
    more updates in prose than the table beneath it shows.
    """
    _, gaps = _contrib()
    assert gaps["real_updates"] == 1
    assert gaps["first_versions"] == 1
    assert gaps["version_rows_total"] == 2


def test_a_download_is_never_retracted_by_the_authors_takedown(seeded):
    """Consumption columns are deliberately outside the rule.

    A download happened whatever later became of the thing downloaded; narrowing
    this by status would report the author's decision as the downloader's activity.
    """
    assert _metric("skill_download").per_eid["ETAKERX"] == 4


def test_the_unattributed_note_describes_what_the_data_shows():
    """The note used to assert something the rows contradict.

    It read "最早的一条在 2026-08-06，之后的记录都带人" — but the unattributed rows run
    to 2026-08-12 and interleave with attributed ones from 08-07, so "之后的记录都带人"
    was false. A wrong explanation in a report is worse than none: the reader takes it
    as a fact about the platform's history when the real cause was that the download
    endpoint read identity from an optional query parameter.
    """
    with SessionLocal() as db:
        metric = {m.key: m for m in figures.collect(db.connection())}["skill_download"]

    assert "之后的记录都带人" not in metric.unattributed_note
    # The cause, not a date: this is what stops the reader concluding the platform
    # simply lacked the field early on.
    assert "查询参数" in metric.unattributed_note
    # And that it cannot be repaired, so nobody goes looking for a backfill.
    assert "无法补回" in metric.unattributed_note
