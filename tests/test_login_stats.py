"""Sign-in statistics: who may read them, and what the windows mean.

Two things are worth pinning down here. First, access: the EID lists say who
used the portal and when, and the sidebar hiding the Dashboard link is not a
control — a signed-in colleague can still call the endpoint. Second, the window
arithmetic: "daily" and "weekly" are the whole feature, and an off-by-one at a
day boundary would quietly misreport for a day without anything looking broken.

The site-wide gate is switched ON here (the rest of the suite runs with
``AUTH_ENABLED=false``), because the endpoint reads the caller's identity from
the token the gate validates.
"""
from datetime import datetime, timedelta, timezone as _timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.login_event import RpAiLoginEvent
from app.models.user import RpAiPortal
from app.routers.login_stats import _db_now, _reporting_tz, _to_db_naive
from app.services.auth_tokens import create_token

SECRET = "login-stats-test-secret"
UTC = _timezone.utc


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", SECRET)
    monkeypatch.setenv("DASHBOARD_EIDS", "EZHGMIO,ADMINMM,EBAOXUE")
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clean_login_events():
    """Each test owns the log: counts are global, so leftovers would leak in."""
    session = SessionLocal()
    session.query(RpAiLoginEvent).delete()
    session.commit()
    session.close()
    yield
    session = SessionLocal()
    session.query(RpAiLoginEvent).delete()
    session.commit()
    session.close()


def _headers(eid: str) -> dict:
    token, _ = create_token(eid, eid.title())
    return {"Authorization": f"Bearer {token}"}


def _login(db, eid: str, when: datetime) -> None:
    """Insert a sign-in at an explicit time (the column defaults to now())."""
    db.add(RpAiLoginEvent(eid=eid, created_at=when))
    db.commit()


# --- Access control ---------------------------------------------------------


@pytest.mark.parametrize("eid", ["EZHGMIO", "ADMINMM", "EBAOXUE"])
def test_the_allowed_eids_can_read_the_stats(client, eid):
    resp = client.get("/api/login-stats", headers=_headers(eid))

    assert resp.status_code == 200


def test_a_signed_in_but_unlisted_user_is_forbidden(client):
    """Not 404 and not empty data: an explicit 403, so the limit is visible."""
    resp = client.get("/api/login-stats", headers=_headers("ESOMEONE"))

    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"].lower()


def test_an_anonymous_caller_is_rejected(client):
    resp = client.get("/api/login-stats")

    assert resp.status_code == 401


def test_the_eid_check_is_case_insensitive(client):
    """Tokens carry whatever case the directory had; the list is uppercase."""
    resp = client.get("/api/login-stats", headers=_headers("ezhgmio"))

    assert resp.status_code == 200


def test_the_allowed_list_is_configurable(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_EIDS", "EOTHER")
    get_settings.cache_clear()

    assert client.get("/api/login-stats", headers=_headers("EZHGMIO")).status_code == 403
    assert client.get("/api/login-stats", headers=_headers("EOTHER")).status_code == 200


# --- Counting ---------------------------------------------------------------


def test_distinct_people_are_counted_once_but_their_logins_add_up(client, db):
    now = _db_now()
    _login(db, "EONE", now - timedelta(minutes=5))
    _login(db, "EONE", now - timedelta(minutes=3))
    _login(db, "ETWO", now - timedelta(minutes=1))

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    assert body["daily"]["count"] == 2
    by_eid = {u["eid"]: u for u in body["daily"]["users"]}
    assert by_eid["EONE"]["logins"] == 2
    assert by_eid["ETWO"]["logins"] == 1
    assert body["totalLogins"] == 3


def test_yesterday_is_not_daily_active(client, db):
    """The day boundary is midnight, not a rolling 24 hours.

    Times are built in the reporting zone, since that is the zone whose midnight
    divides the days — building them on the DB clock would put "yesterday 23:00"
    on the wrong side of the boundary when the two zones differ.

    Whether yesterday is also *weekly* active depends on the weekday: on a
    Monday, yesterday was Sunday and therefore last week. That is the ISO-week
    definition doing its job, so the assertion follows the same rule instead of
    assuming a rolling window.
    """
    now_local = datetime.now(_reporting_tz())
    yesterday_local = (now_local - timedelta(days=1)).replace(
        hour=23, minute=0, second=0, microsecond=0
    )
    _login(db, "EYESTERDAY", _to_db_naive(yesterday_local))
    _login(db, "ETODAY", _db_now() - timedelta(minutes=1))

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    assert [u["eid"] for u in body["daily"]["users"]] == ["ETODAY"]

    weekly = {u["eid"] for u in body["weekly"]["users"]}
    assert "ETODAY" in weekly
    yesterday_is_same_week = now_local.weekday() != 0  # Monday -> yesterday was last week
    assert ("EYESTERDAY" in weekly) is yesterday_is_same_week


def test_a_login_just_after_midnight_today_is_daily_active(client, db):
    """The inclusive edge of the window — the easiest place to be off by one."""
    now = _db_now()
    just_after_midnight = now.replace(hour=0, minute=0, second=1, microsecond=0)
    _login(db, "EEARLY", just_after_midnight)

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    assert [u["eid"] for u in body["daily"]["users"]] == ["EEARLY"]


def test_the_day_starts_at_midnight_in_the_users_time_zone(client, db, monkeypatch):
    """Not the DB server's midnight: the DB is UTC+2, the team is UTC+8.

    A sign-in at 02:00 Beijing is still "today" for the people reading this
    page, even though it is 20:00 of the previous day where the database lives.
    """
    monkeypatch.setenv("STATS_TIMEZONE", "Asia/Shanghai")
    get_settings.cache_clear()

    beijing = ZoneInfo("Asia/Shanghai")
    now_beijing = datetime.now(beijing)
    two_am_beijing = now_beijing.replace(hour=2, minute=0, second=0, microsecond=0)
    if two_am_beijing > now_beijing:  # before 02:00 locally — use yesterday's
        pytest.skip("run between 02:00 and midnight Beijing time")
    _login(db, "ENIGHTOWL", _to_db_naive(two_am_beijing))

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    assert [u["eid"] for u in body["daily"]["users"]] == ["ENIGHTOWL"]
    # And the window boundary itself is reported as that zone's midnight.
    assert body["daily"]["since"] == (
        two_am_beijing.replace(hour=0).astimezone(UTC).isoformat()
    )


def test_an_unknown_time_zone_does_not_break_the_page(client, db, monkeypatch):
    """A missing tz database must degrade, not 500."""
    monkeypatch.setenv("STATS_TIMEZONE", "Not/AZone")
    get_settings.cache_clear()
    _login(db, "EONE", _db_now() - timedelta(minutes=1))

    resp = client.get("/api/login-stats", headers=_headers("EZHGMIO"))

    assert resp.status_code == 200
    assert resp.json()["daily"]["count"] == 1


def test_eight_days_ago_is_outside_this_week(client, db):
    """Anything before this Monday is out, however recent it feels."""
    now_local = datetime.now(_reporting_tz())
    monday = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    _login(db, "ELASTWEEK", _to_db_naive(monday - timedelta(minutes=1)))
    _login(db, "ETHISWEEK", _to_db_naive(monday + timedelta(minutes=1)))

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    weekly = {u["eid"] for u in body["weekly"]["users"]}
    assert "ETHISWEEK" in weekly
    assert "ELASTWEEK" not in weekly
    # Still counted in the all-time total: the event was not discarded.
    assert body["totalLogins"] == 2


def test_the_week_starts_on_monday(client, db):
    """Monday–Sunday, not a rolling 7 days and not Sunday-first.

    Pinned because the difference is invisible in the number itself: only the
    reported ``since`` says which convention produced it.
    """
    now_local = datetime.now(_reporting_tz())
    expected_monday = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    since = datetime.fromisoformat(body["weekly"]["since"])
    assert since == expected_monday
    # 0 == Monday. A rolling window would land on today's weekday instead.
    assert since.astimezone(_reporting_tz()).weekday() == 0
    assert since.astimezone(_reporting_tz()).hour == 0


def test_last_sunday_belongs_to_the_previous_week(client, db):
    """The boundary case that a rolling window would get wrong.

    Sunday is the *last* day of an ISO week, so the Sunday before this Monday is
    last week — even though it is at most six days ago and a rolling 7-day
    window would include it.
    """
    now_local = datetime.now(_reporting_tz())
    monday = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    last_sunday_evening = monday - timedelta(hours=1)  # Sunday 23:00
    _login(db, "ESUNDAY", _to_db_naive(last_sunday_evening))

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    assert body["weekly"]["count"] == 0
    assert body["daily"]["count"] == 0
    assert body["totalLogins"] == 1


def test_no_activity_yields_zeroes_not_an_error(client):
    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    assert body["daily"] == {**body["daily"], "count": 0, "users": []}
    assert body["weekly"]["count"] == 0
    assert body["totalLogins"] == 0
    assert body["recordingSince"] is None


def test_the_display_name_comes_from_the_user_row(client, db):
    db.add(RpAiPortal(eid="ENAMED", display_name="Named Person", department="RP AI"))
    db.commit()
    _login(db, "ENAMED", _db_now() - timedelta(minutes=1))

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    user = body["daily"]["users"][0]
    assert user["displayName"] == "Named Person"
    assert user["department"] == "RP AI"

    db.query(RpAiPortal).filter(RpAiPortal.eid == "ENAMED").delete()
    db.commit()


def test_someone_without_a_user_row_still_counts(client, db):
    """A missing user row must not make a sign-in disappear from the count."""
    _login(db, "EGHOST", _db_now() - timedelta(minutes=1))

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    assert body["daily"]["count"] == 1
    assert body["daily"]["users"][0]["eid"] == "EGHOST"
    assert body["daily"]["users"][0]["displayName"] is None


def test_users_are_ordered_by_most_recent_login(client, db):
    now = _db_now()
    _login(db, "EOLDEST", now - timedelta(hours=3))
    _login(db, "ENEWEST", now - timedelta(minutes=1))
    _login(db, "EMIDDLE", now - timedelta(hours=1))

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    assert [u["eid"] for u in body["daily"]["users"]] == ["ENEWEST", "EMIDDLE", "EOLDEST"]


def test_timestamps_are_emitted_as_utc(client, db):
    """Naive DB-local values must be labelled, or the browser shifts them."""
    _login(db, "EONE", _db_now() - timedelta(minutes=1))

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()

    for value in (body["daily"]["since"], body["daily"]["users"][0]["lastLoginAt"]):
        assert value.endswith("+00:00") or value.endswith("Z")


# --- Recording --------------------------------------------------------------


def test_signing_in_records_an_event(client, db):
    """The counts are only as good as the write on the login path.

    Uses the break-glass admin, which authenticates without LDAP, so this covers
    the real endpoint end to end without a directory.
    """
    resp = client.post(
        "/api/auth/login", json={"username": "adminmm", "password": "adminmm"}
    )
    assert resp.status_code == 200

    events = db.query(RpAiLoginEvent).all()
    assert [e.eid for e in events] == ["ADMINMM"]
    assert events[0].created_at is not None

    body = client.get("/api/login-stats", headers=_headers("EZHGMIO")).json()
    assert [u["eid"] for u in body["daily"]["users"]] == ["ADMINMM"]


def test_a_failed_sign_in_records_nothing(client, db):
    """Only successful sign-ins are activity; refused attempts are not."""
    resp = client.post(
        "/api/auth/login", json={"username": "adminmm", "password": "wrong"}
    )
    assert resp.status_code in (401, 503)

    assert db.query(RpAiLoginEvent).count() == 0
