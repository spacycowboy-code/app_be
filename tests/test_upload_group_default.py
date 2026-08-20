"""Uploads land in the uploader's own group, and a bad group is refused.

Before this, ``categoryId`` was whatever the client sent: an unknown slug was
written straight into the skill row, the manifest and the git path, and a skill
filed under a category nobody filters by is invisible everywhere except "All".
There was also no default, even though the uploader's LDAP department already
says which group they belong to.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.category import RpAiCategory
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.routers import upload as upload_mod
from app.services.auth_tokens import create_token
from app.services.audit_engine import AuditResult
from app.services.skill_storage import StoreResult

SKILL_MD = "---\nname: T\ndescription: d\n---\n# T\nbody\n"

IVC_USER = "EIVCONE"  # department maps to the 'ivc' group
CHENGDU_USER = "ECDONE"  # Chengdu: mapped to nothing, by decision


@pytest.fixture()
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "group-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    monkeypatch.setattr(
        upload_mod.skill_storage,
        "store",
        lambda **kw: StoreResult("ivc/x", "1.0.0", "abc123", None, None, False),
    )
    monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: AuditResult(state="pass"))
    with SessionLocal() as db:
        for slug, label in (("ivc", "IVC"), ("public", "Public"), ("official", "Official")):
            if db.get(RpAiCategory, slug) is None:
                db.add(RpAiCategory(id=slug, label=label, count=0))
        for eid, dept in (
            (IVC_USER, "BNEW RTE RP IVC TE Test Environment"),
            (CHENGDU_USER, "BNEW RTE RP CD SW1 Software 1"),
        ):
            row = db.get(RpAiPortal, eid) or db.query(RpAiPortal).filter(
                RpAiPortal.eid == eid
            ).one_or_none()
            if row is None:
                db.add(RpAiPortal(eid=eid, display_name=eid, department=dept))
            else:
                row.department = dept
        db.commit()
    return TestClient(app)


def _auth(eid: str) -> dict:
    token, _ = create_token(eid, display_name=eid)
    return {"Authorization": f"Bearer {token}"}


def _upload(client, eid: str, name: str, category_id: str | None):
    data = {"eid": eid, "name": name}
    if category_id is not None:
        data["categoryId"] = category_id
    return client.post(
        "/api/skills/upload",
        data=data,
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
        headers=_auth(eid),
    )


def _category_of(name: str) -> str | None:
    with SessionLocal() as db:
        return db.query(RpAiSkill).filter(RpAiSkill.name == name).one().category_id


def test_an_empty_group_falls_back_to_the_uploaders_department(client):
    assert _upload(client, IVC_USER, "group-default", "").status_code == 201
    assert _category_of("group-default") == "ivc"


def test_an_explicit_group_still_wins(client):
    """The field is a default, not a rule: a skill can belong somewhere else."""
    assert _upload(client, IVC_USER, "group-explicit", "public").status_code == 201
    assert _category_of("group-explicit") == "public"


def test_an_unknown_group_is_refused_rather_than_stored(client):
    resp = _upload(client, IVC_USER, "group-unknown", "not-a-group")
    assert resp.status_code == 400
    assert "not-a-group" in resp.text
    with SessionLocal() as db:
        assert db.query(RpAiSkill).filter(RpAiSkill.name == "group-unknown").count() == 0


@pytest.mark.parametrize("pseudo", ["all", "official"])
def test_filter_and_synced_categories_are_not_upload_targets(client, pseudo):
    """'all' is a filter; 'official' means vetted by us and is filled by the sync."""
    resp = _upload(client, IVC_USER, f"group-{pseudo}", pseudo)
    assert resp.status_code == 400, resp.text


def test_an_unmapped_department_must_choose(client):
    """Chengdu maps to no group: better to ask than to file it somewhere wrong."""
    resp = _upload(client, CHENGDU_USER, "group-unmapped", "")
    assert resp.status_code == 400
    assert "does not map" in resp.text
    # ...but they can still upload by naming a group themselves.
    assert _upload(client, CHENGDU_USER, "group-chosen", "public").status_code == 201
    assert _category_of("group-chosen") == "public"


def test_me_reports_the_group_so_the_ui_can_default_to_it(client):
    body = client.get("/api/auth/me", headers=_auth(IVC_USER)).json()
    assert body["group"] == "ivc"
    assert body["groupLabel"] == "IVC"
    assert body["department"] == "BNEW RTE RP IVC TE Test Environment"

    unmapped = client.get("/api/auth/me", headers=_auth(CHENGDU_USER)).json()
    assert unmapped["group"] is None
    assert unmapped["groupLabel"] is None
    # The raw path is still returned: the UI says where the (absent) group came from.
    assert unmapped["department"] == "BNEW RTE RP CD SW1 Software 1"


def test_a_group_whose_category_was_removed_is_not_reported(client):
    """The mapping is code and categories are data; they can drift."""
    with SessionLocal() as db:
        db.delete(db.get(RpAiCategory, "ivc"))
        db.commit()
    try:
        body = client.get("/api/auth/me", headers=_auth(IVC_USER)).json()
        assert body["group"] is None, "a slug with no category row would filter to nothing"
    finally:
        with SessionLocal() as db:
            db.add(RpAiCategory(id="ivc", label="IVC", count=0))
            db.commit()
