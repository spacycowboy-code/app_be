"""Which assets the department report counts as a contribution.

The report had no status filter at all: every row in ``rp_ai_skill`` and
``rp_ai_market_item`` counted, whatever had become of it. Two consequences, and
only one of them was defensible.

A skill that was taken down still counted, and should — it was live and
downloadable first, ``skills.delete_skill`` keeps its row for exactly that
reason, and the download columns go on counting the downloads it received. Drop
it from the upload column and a department can show downloads with nobody who
ever uploaded anything.

A skill still in review, or one whose review was rejected, also counted, and
should not: the public list filters on ``approved``, so neither was ever visible
to anyone. That turns "uploaded" into "attempted to upload" — and for rejected
submissions it is a number someone can drive, since ten packages that all failed
review would read as ten uploads.

These tests pin both halves, plus the two figures that have to move together with
the update column so the prose explaining it cannot contradict the table.
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


def test_upload_column_counts_live_and_taken_down_only(seeded):
    """approved + archived = 2. The pending and rejected ones are not uploads yet."""
    assert _metric("skill_upload").per_eid[AUTHOR] == 2


def test_update_column_follows_the_same_rule(seeded):
    """One update each from the live and the taken-down skill, and nothing else."""
    assert _metric("skill_update").per_eid[AUTHOR] == 2


def test_market_upload_column_excludes_items_still_under_review(seeded):
    """Market has no archived state, so only the two approved items count."""
    assert _metric("agent_upload").per_eid[AUTHOR] == 2
    assert _metric("market_item_upload").per_eid[AUTHOR] == 2


def test_contribution_table_agrees_with_the_columns(seeded):
    """The contribution list is the same population, or the report contradicts itself."""
    rows, _ = _contrib()
    assert rows[AUTHOR]["skills"] == 2
    assert rows[AUTHOR]["versions"] == 2
    assert rows[AUTHOR]["market_items"] == 2


def test_downloads_of_a_taken_down_skill_still_count(seeded):
    """The reason archived has to stay in: its downloads are counted either way.

    Excluding archived from the upload column while these downloads keep counting
    is what would let a department show take-ups with no uploader behind them.
    """
    rows, _ = _contrib()
    assert rows[AUTHOR]["skill_taken"] == 2  # live + taken-down
    assert rows[AUTHOR]["skill_takers"] == 1


def test_the_prose_figures_move_with_the_update_column(seeded):
    """``real_updates`` explains the update column, so it must count the same rows.

    Left unfiltered, a rejected skill's version rows would make the report state
    more updates in prose than the table beneath it shows.
    """
    _, gaps = _contrib()
    assert gaps["real_updates"] == 2
    assert gaps["first_versions"] == 2
    assert gaps["version_rows_total"] == 4


def test_a_download_is_never_retracted_by_the_authors_takedown(seeded):
    """Consumption columns are deliberately outside the rule.

    A download happened whatever later became of the thing downloaded; narrowing
    this by status would report the author's decision as the downloader's activity.
    """
    assert _metric("skill_download").per_eid["ETAKERX"] == 4
