"""Test config: hermetic sqlite DB + FastAPI TestClient.

External dependencies (Kiro judge, git storage, GitLab MR) are stubbed per-test
via monkeypatch, so the suite is deterministic and needs no network/credentials.
"""
import os
import tempfile

# Force a hermetic sqlite DB even if the environment (e.g. CI) exports a
# DATABASE_URL — setdefault would NOT override it, so assign directly.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.gettempdir()}/_audit_test.db"
os.environ["SKILL_REPO_PUSH"] = "false"
# The hermetic suite exercises endpoint/business logic, not the auth gate, so
# turn the site-wide JWT gate off here (its behavior is verified separately).
os.environ["AUTH_ENABLED"] = "false"
os.environ.setdefault("SKILL_REPO_PATH", tempfile.mkdtemp())
os.environ.setdefault("AUDIT_ENABLED", "true")
os.environ.setdefault("AUDIT_ADMIN_EID", "ADMINMM")
os.environ["AUDIT_JUDGE_URL"] = ""  # never call a remote judge from tests
# Don't shell out to the Cisco scanner in hermetic unit tests; its mapping is
# unit-tested directly and its integration is covered on the deployed stack.
os.environ["AUDIT_CISCO_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

import app.models  # noqa: E402,F401  (register models)
from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


# sqlite ignores foreign keys unless asked; production runs MySQL, which does
# enforce them. Turn them on so constraint mistakes fail here and not in
# production.
@event.listens_for(engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # Skills reference a category by foreign key, and a real deployment always
    # has its categories seeded, so create the one the tests upload into.
    from app.database import SessionLocal
    from app.models.category import RpAiCategory
    with SessionLocal() as db:
        db.add(RpAiCategory(id="public", label="Public"))
        db.commit()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client():
    return TestClient(app)
