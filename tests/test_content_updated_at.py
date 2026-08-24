"""``contentUpdatedAt``: when a skill's content changed, not when its row did.

The Home feed calls a skill "Updated" when that timestamp is newer than
``createdAt``. It used to read ``updated_at`` straight off the skill row, which
is a bookkeeping column: it moves on *any* write, so download counters, ratings
and repo bookkeeping all announced untouched skills as updated.

``content_updated_at`` is derived from ``rp_ai_skill_version`` instead — one row
per upload, the only authoritative record of a content change. Skills with no
version history (the official imports) fall back to ``updated_at``.

Every timestamp here is derived from a value the *database* wrote, never from
``datetime.now()``: sqlite's ``CURRENT_TIMESTAMP`` is UTC while Python's local
clock here is UTC+8, so mixing the two silently puts the fixtures hours away
from the rows they are meant to describe.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.database import SessionLocal
from app.main import app
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.models.version import RpAiSkillVersion

SKILL_ID = "content-updated-probe"
OWNER_EID = "EZHGMIO"
BACKDATE = timedelta(hours=2)


@pytest.fixture(autouse=True)
def _content_in_repo(monkeypatch):
    """详情接口与"只改元数据"的编辑都按引用从仓库读正文,所以测试要给一份。

    这不是补丁:正文的权威在仓库,读不到就该失败。这些测试关心的是时间戳与审计,不是正文本身。
    """
    from app.repositories import skill_content

    monkeypatch.setattr(
        skill_content, "read_text",
        lambda ref, path: "---\nname: Probe\ndescription: probe\n---\n\n# body\n")


@pytest.fixture
def content_in_repo(monkeypatch):
    """下载的字节来自技能那一行记着的引用,不再来自 ``instructions``。

    这些测试关心的是下载计数与时间戳,不是内容,所以仓库那一版用最小的一份就够。
    """
    from app.repositories import skill_content

    monkeypatch.setattr(
        skill_content, "read_tree", lambda ref, prefix: {"SKILL.md": b"# Probe\n"}
    )


@pytest.fixture
def client():
    return TestClient(app)


def _seed(*, versions: list[timedelta]) -> datetime:
    """Create the skill two hours ago, plus a version row per offset given.

    Offsets are measured from the skill's creation, so ``[]`` means "no version
    history at all" (an official import) and ``[0s, 1h]`` means "created, then
    re-uploaded an hour later".

    The row is inserted first so the DB stamps it on its own clock, then moved
    into the past with a Core UPDATE that names ``updated_at`` — naming it is
    what stops ``TimestampMixin.onupdate`` from overwriting the planted value.
    """
    with SessionLocal() as db:
        if db.get(RpAiPortal, OWNER_EID) is None:
            db.add(RpAiPortal(eid=OWNER_EID, display_name="Miao Zhang M"))
        db.add(
            RpAiSkill(
                id=SKILL_ID,
                name="Content Updated Probe",
                category_id="public",
                status="approved",
                version="1.0.0",
                tags=[],
                storage_path="team/content-updated-probe_EXXX_Probe",
                repo_commit="c" * 40,
                instructions="# Probe\n\nA skill body so the download has content.\n",
            )
        )
        db.commit()

        row = db.get(RpAiSkill, SKILL_ID)
        assert row is not None
        created = row.created_at - BACKDATE
        db.execute(
            update(RpAiSkill)
            .where(RpAiSkill.id == SKILL_ID)
            .values(created_at=created, updated_at=created)
        )
        for offset in versions:
            db.add(
                RpAiSkillVersion(
                    skill_id=SKILL_ID,
                    version="1.0.0",
                    uploader_eid=OWNER_EID,
                    created_at=created + offset,
                )
            )
        db.commit()
    return created


def _summary(client: TestClient) -> dict:
    resp = client.get("/api/skills")
    assert resp.status_code == 200, resp.text
    rows = [s for s in resp.json() if s["id"] == SKILL_ID]
    assert rows, f"{SKILL_ID} missing from the list response"
    return rows[0]


def _is_update_on_home(row: dict) -> bool:
    """Mirror of the Home feed's rule (home.component.ts)."""
    changed = row.get("contentUpdatedAt") or row.get("updatedAt")
    return bool(changed and row.get("createdAt") and changed > row["createdAt"])


def test_a_skill_uploaded_once_is_not_an_update(client):
    """One version row, laid down at creation: nothing has changed since."""
    _seed(versions=[timedelta(0)])

    row = _summary(client)

    assert row["contentUpdatedAt"] == row["createdAt"]
    assert not _is_update_on_home(row)


def test_a_first_upload_whose_version_row_lands_later_is_not_an_update(client):
    """The row and its version row are not written at the same instant.

    ``upload.py`` flushes the skill on its own, then adds the version row, which
    is only INSERTed at commit — after the audit record and, for a suspect
    upload, after a GitLab MR round-trip. Each INSERT evaluates
    ``CURRENT_TIMESTAMP`` separately, so the version row is stamped seconds after
    the skill. Comparing the two directly would announce every brand-new skill as
    "Updated" the moment it appears.

    The row is also churned here so that every wrong answer is caught: reporting
    the version row's stamp fails, and so does falling back to ``updated_at``.
    """
    _seed(versions=[timedelta(seconds=5)])
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, SKILL_ID)
        assert skill is not None
        skill.downloads = "1"  # someone downloads it right after it appears
        db.commit()

    row = _summary(client)

    assert row["updatedAt"] > row["createdAt"], "precondition: the row was churned"
    assert row["contentUpdatedAt"] == row["createdAt"], (
        "a single upload counts as creation, not as an update, however long the "
        "version row took to land"
    )
    assert not _is_update_on_home(row)


def test_a_re_upload_is_an_update(client):
    """A second version row is a real content change and must still surface."""
    _seed(versions=[timedelta(0), timedelta(hours=1)])

    row = _summary(client)

    assert row["contentUpdatedAt"] > row["createdAt"]
    assert _is_update_on_home(row)


def test_a_download_does_not_make_a_skill_an_update(client, content_in_repo):
    """The original bug, asserted through the field the feed actually reads."""
    _seed(versions=[timedelta(0)])
    before = _summary(client)

    resp = client.get(f"/api/skills/{SKILL_ID}/download")
    assert resp.status_code == 200, resp.text

    after = _summary(client)
    assert after["contentUpdatedAt"] == before["contentUpdatedAt"], (
        "a download moved the content timestamp -> it will show up as an "
        '"Update" on the Home feed'
    )
    assert not _is_update_on_home(after)


def test_a_row_write_does_not_make_a_skill_an_update(client):
    """Any future bookkeeping write must not reach ``contentUpdatedAt``.

    Rather than guess which endpoint might churn the row next, this moves
    ``updated_at`` directly — standing in for whatever write comes along later —
    and pins that the feed still stays quiet.
    """
    created = _seed(versions=[timedelta(0)])
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, SKILL_ID)
        assert skill is not None
        skill.downloads = "1"  # ORM write: fires TimestampMixin.onupdate
        db.commit()
        assert skill.updated_at > created, "precondition: the row was churned"

    row = _summary(client)

    assert row["updatedAt"] > row["createdAt"], "precondition: updatedAt moved"
    assert row["contentUpdatedAt"] == row["createdAt"], (
        "contentUpdatedAt followed a bookkeeping write to the row"
    )
    assert not _is_update_on_home(row)


def test_an_official_skill_without_version_rows_falls_back_to_updated_at(client):
    """Official imports keep no version history, so they must not go silent.

    Their ``updated_at`` is trustworthy now that the import writes a per-folder
    commit sha; falling back to it keeps genuine official updates visible instead
    of hiding every official skill from the feed forever.
    """
    _seed(versions=[])
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, SKILL_ID)
        assert skill is not None
        skill.description = "the official repo changed this skill"
        db.commit()

    row = _summary(client)

    assert row["contentUpdatedAt"] == row["updatedAt"]
    assert row["contentUpdatedAt"] > row["createdAt"]
    assert _is_update_on_home(row)


def test_the_timestamps_are_labelled_the_same_way(client):
    """All three must be UTC-labelled, because the feed compares them as strings.

    An unlabelled ``2026-08-18T22:04:40`` sorts as *less than* the identical
    labelled ``2026-08-18T22:04:40+00:00``, so a mismatch here would quietly stop
    real updates from being recognised rather than fail loudly.
    """
    _seed(versions=[timedelta(0), timedelta(hours=1)])

    row = _summary(client)

    for key in ("createdAt", "updatedAt", "contentUpdatedAt"):
        assert row[key].endswith("+00:00") or row[key].endswith("Z"), (
            f"{key} is not labelled as UTC: {row[key]}"
        )


def test_the_detail_endpoint_reports_the_same_timestamp(client):
    """The card and the detail page must not disagree about being updated."""
    _seed(versions=[timedelta(0), timedelta(hours=1)])

    summary = _summary(client)
    detail = client.get(f"/api/skills/{SKILL_ID}").json()

    assert detail["contentUpdatedAt"] == summary["contentUpdatedAt"]
