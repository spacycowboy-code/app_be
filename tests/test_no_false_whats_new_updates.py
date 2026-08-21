"""Only a real content update may mark a skill as updated.

The Home page's "What's New" feed reports a skill as updated whenever
``updated_at > created_at`` (see ``home.component.ts``), and
``TimestampMixin.updated_at`` carries ``onupdate=func.now()`` — so it moves on
ANY update statement that does not name the column. Three activities used to
trip that wire without the skill's content or ``version`` changing at all:

* downloading it (the ``downloads`` counter),
* someone rating it (``rating`` / ``review_count``),
* the official-skill sync writing the repo-wide HEAD sha into every official
  skill's ``repo_commit``.

Each is covered below. Timestamps are seeded an hour into the past so the
assertions hold even where the clock has one-second granularity (sqlite's
``CURRENT_TIMESTAMP``): a stray ``onupdate`` jumps a whole hour, not a few ms.
"""
import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.download import RpAiSkillDownload
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal

SKILL_ID = "download-timestamp-probe"
REVIEWER_EID = "EZHGMIO"
BACKDATE = timedelta(hours=1)


@pytest.fixture
def content_in_repo(monkeypatch):
    """下载的字节来自技能那一行记着的引用,不再来自 ``instructions``。

    这些测试关心的是下载计数与时间戳,不是内容,所以仓库那一版用最小的一份就够。
    """
    from app.repositories import skill_content

    monkeypatch.setattr(
        skill_content, "read_tree", lambda ref, prefix: {"SKILL.md": b"# Probe\n"}
    )


def _seed(downloads: str = "0", *, skill_id: str = SKILL_ID) -> datetime:
    """Create the skill with timestamps an hour old; return that timestamp.

    A reviewer user is seeded too: ``rp_ai_skill_review.reviewer_eid`` is a
    foreign key into ``rp_ai_portal`` and the suite enforces sqlite FKs.
    """
    stamp = datetime.now().replace(microsecond=0) - BACKDATE
    with SessionLocal() as db:
        if db.get(RpAiPortal, REVIEWER_EID) is None:
            db.add(RpAiPortal(eid=REVIEWER_EID, display_name="Miao Zhang M"))
        db.add(
            RpAiSkill(
                id=skill_id,
                name="Download Timestamp Probe",
                category_id="public",
                downloads=downloads,
                version="1.0.0",
                rating=0.0,
                review_count=0,
                storage_path="team/download-timestamp-probe_EXXX_Probe",
                repo_commit="e" * 40,
                instructions="# Probe\n\nA skill body so the download has content.\n",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        db.commit()
    return stamp


def _tick() -> None:
    """Wait until a second-resolution clock will report a different value.

    sqlite's ``CURRENT_TIMESTAMP`` (and MySQL's ``DATETIME`` here, precision 0)
    keeps whole seconds, so two writes inside the same second are
    indistinguishable. That cuts both ways: "a real edit still counts" fails on
    two identical stamps, and "an unrelated commit changes nothing" passes even
    when a stray ``onupdate`` did fire, because the jump is invisible.

    Waiting is deliberately used instead of back-dating the row: back-dating has
    to pick a clock, and the two here disagree — ``datetime.now()`` is local
    (UTC+8) while sqlite writes UTC, so an hour "ago" in Python is seven hours
    ahead of anything the database generates.
    """
    time.sleep(1.05)


def _row(skill_id: str = SKILL_ID):
    with SessionLocal() as db:
        s = db.get(RpAiSkill, skill_id)
        assert s is not None
        return {
            "downloads": s.downloads,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "version": s.version,
            "rating": s.rating,
            "review_count": s.review_count,
            "repo_commit": s.repo_commit,
        }


def _download_events() -> list[str | None]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(RpAiSkillDownload.eid).where(
                    RpAiSkillDownload.skill_id == SKILL_ID
                )
            ).all()
        )


# --- downloading ----------------------------------------------------------


def test_download_increments_counter_without_touching_updated_at(client, content_in_repo):
    seeded = _seed("7")

    resp = client.get(f"/api/skills/{SKILL_ID}/download", params={"eid": "EZHGMIO"})
    assert resp.status_code == 200
    assert resp.content[:2] == b"PK"  # a real zip came back

    row = _row()
    assert row["downloads"] == "8", "counter must still increment"
    assert row["version"] == "1.0.0", "a download never changes the version"
    assert row["updated_at"] == seeded, (
        "download moved updated_at -> the skill will show up as an "
        f"\"Update\" on the Home feed (was {seeded}, now {row['updated_at']})"
    )
    assert not (row["updated_at"] > row["created_at"])  # the feed's own predicate
    assert _download_events() == ["EZHGMIO"]


def test_anonymous_download_is_logged_without_eid(client, content_in_repo):
    seeded = _seed("0")

    assert client.get(f"/api/skills/{SKILL_ID}/download").status_code == 200

    row = _row()
    assert row["downloads"] == "1"
    assert row["updated_at"] == seeded
    assert _download_events() == [None]


@pytest.mark.parametrize("formatted", ["5.6K", "1.2M"])
def test_non_numeric_counter_keeps_event_row_and_timestamp(client, formatted, content_in_repo):
    """A display-formatted counter can't be incremented; the event row and the
    timestamp must both survive that."""
    seeded = _seed(formatted)

    resp = client.get(f"/api/skills/{SKILL_ID}/download", params={"eid": "ADMINMM"})
    assert resp.status_code == 200

    row = _row()
    assert row["downloads"] == formatted, "unparseable counter is left alone"
    assert row["updated_at"] == seeded
    assert _download_events() == ["ADMINMM"]


# --- rating ---------------------------------------------------------------


def _review_body(score: int) -> dict:
    return {
        "reviewerEid": REVIEWER_EID,
        "accuracy": score,
        "value": score,
        "docs": score,
        "safety": score,
        "comment": "solid",
    }


def test_posting_a_review_aggregates_without_touching_updated_at(client):
    seeded = _seed()

    resp = client.post(f"/api/skills/{SKILL_ID}/reviews", json=_review_body(4))
    assert resp.status_code == 201, resp.text

    row = _row()
    assert row["review_count"] == 1, "the aggregate must still be recomputed"
    assert row["rating"] == pytest.approx(4.0)
    assert row["version"] == "1.0.0", "a review never changes the version"
    assert row["updated_at"] == seeded, (
        "rating a skill moved updated_at -> it will show up as an \"Update\" "
        f"on the Home feed (was {seeded}, now {row['updated_at']})"
    )


def test_second_review_averages_and_still_leaves_the_timestamp(client):
    seeded = _seed()

    assert client.post(
        f"/api/skills/{SKILL_ID}/reviews", json=_review_body(5)
    ).status_code == 201
    assert client.post(
        f"/api/skills/{SKILL_ID}/reviews", json=_review_body(3)
    ).status_code == 201

    row = _row()
    assert row["review_count"] == 2
    assert row["rating"] == pytest.approx(4.0), "running average across reviews"
    assert row["updated_at"] == seeded


# --- official-skill sync --------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "-c", f"safe.directory={repo}",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_official_skill(repo: Path, skill_id: str, version: str) -> Path:
    folder = repo / "official" / "anthropics" / skill_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(
        json.dumps({"id": skill_id, "name": skill_id, "version": version}),
        encoding="utf-8",
    )
    (folder / "SKILL.md").write_text(f"# {skill_id}\n", encoding="utf-8")
    return folder


@pytest.fixture
def official_repo(tmp_path, monkeypatch):
    """A throwaway skill repo with two official skills, each its own commit."""
    from app.services import official_import
    from app.services.skill_storage import skill_storage

    repo = tmp_path / "skill-repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")

    _write_official_skill(repo, "alpha", "1.0.0")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add alpha")

    _write_official_skill(repo, "beta", "2.0.0")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add beta")

    monkeypatch.setattr(skill_storage, "repo", repo)
    monkeypatch.setattr(official_import.settings, "skill_repo_path", str(repo))
    return repo


def _import(repo: Path) -> dict:
    from app.services.official_import import import_official_skills

    with SessionLocal() as db:
        # refresh=False: no fetch/ff against a remote this repo does not have.
        summary = import_official_skills(db, refresh=False)
    return summary


def test_official_import_records_the_commit_that_touched_each_skill(official_repo):
    _import(official_repo)

    alpha, beta = _row("alpha"), _row("beta")
    assert alpha["repo_commit"] and beta["repo_commit"]
    assert alpha["repo_commit"] != beta["repo_commit"], (
        "repo_commit must identify the commit that last changed THIS skill; "
        "identical values mean the repo-wide HEAD was written to every row"
    )


def test_an_unrelated_commit_does_not_mark_official_skills_as_updated(official_repo):
    """The bug: any commit to the skill repo — including the backend's own
    upload commits for unrelated user skills — moved every official skill's
    repo_commit, so all of them surfaced as "Update" cards."""
    _import(official_repo)
    before = {sid: _row(sid) for sid in ("alpha", "beta")}

    # Something lands in the repo that has nothing to do with either skill.
    (official_repo / "unrelated.txt").write_text("user upload\n", encoding="utf-8")
    _git(official_repo, "add", "-A")
    _git(official_repo, "commit", "-m", "an unrelated user-skill upload")

    _tick()
    _import(official_repo)

    for sid in ("alpha", "beta"):
        after = _row(sid)
        assert after["repo_commit"] == before[sid]["repo_commit"], (
            f"{sid}: repo_commit moved on a commit that did not touch it"
        )
        assert after["updated_at"] == before[sid]["updated_at"], (
            f"{sid}: updated_at moved -> it will show up as an \"Update\" on the "
            f"Home feed (was {before[sid]['updated_at']}, now {after['updated_at']})"
        )
        assert after["version"] == before[sid]["version"]


def test_editing_a_skill_does_move_its_own_commit_and_timestamp(official_repo):
    """The flip side: a real change to the skill's own files must still register,
    otherwise the fix would hide genuine updates."""
    _import(official_repo)
    before = _row("alpha")

    _write_official_skill(official_repo, "alpha", "1.1.0")
    _git(official_repo, "add", "-A")
    _git(official_repo, "commit", "-m", "bump alpha to 1.1.0")

    _tick()
    _import(official_repo)

    after = _row("alpha")
    assert after["repo_commit"] != before["repo_commit"], "alpha's own commit moved"
    assert after["version"] == "1.1.0", "the new version must land"
    assert after["updated_at"] > before["updated_at"], (
        "a genuine content change must still mark the skill as updated"
    )
