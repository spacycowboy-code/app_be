"""下载记录里的人是谁，由已认证的调用者决定，不由查询参数决定。

改之前 ``download_skill`` 的签名是 ``eid: str | None = Query(default=None)`` —— 它是这条
路径上**唯一**的身份来源，而市场那侧同一个动作早就走 ``acting_eid(request, eid)`` 了。两个
后果都在生产上出现过：

* 调用方不带 ``?eid=`` 时，写进去的行没有人。生产库里有十条这样的记录（2026-08-06 至
  08-12），报告里专门有一句话解释它们无人可归。它们补不回来 —— 当时的身份从未被记录，
  而 ``rp_ai_login_event`` 最早 08-17、``rp_ai_usage_event`` 最早 08-21，没有任何表覆盖
  那段时间。
* 调用方带上别人的工号时，这次下载就记在别人头上。对一个被报告当作「谁在用什么」呈现的
  数字来说，这一条比前一条更严重。

这里钉住的是修好之后的行为：身份取自已认证的调用者；查询参数只在没有认证身份时兜底
（开发环境关掉鉴权时仍要能用）；两者都没有时不写出一个假的归属。
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.download import RpAiSkillDownload
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.repositories import skill_content
from app.services.auth_tokens import create_token

REF = "e" * 40
PATH = "team/attribution-probe_EXXX_Attribution_Probe"
SKILL_ID = "attribution-probe"
OWNER = "EOWNERX"
CALLER = "ECALLER"
VICTIM = "EVICTIM"


@pytest.fixture
def probe(monkeypatch):
    with SessionLocal() as db:
        # ``uploader_eid`` is a real foreign key, and the download path resolves the
        # caller's department from the same table, so both people need a user row.
        for eid in (OWNER, CALLER, VICTIM):
            if db.get(RpAiPortal, eid) is None and not db.query(RpAiPortal).filter(
                RpAiPortal.eid == eid
            ).first():
                db.add(RpAiPortal(eid=eid, display_name=eid.title(), department="QA"))
        db.flush()
        db.add(
            RpAiSkill(
                id=SKILL_ID,
                name="Attribution Probe",
                category_id="public",
                status="approved",
                version="1.0.0",
                tags=[],
                storage_path=PATH,
                repo_commit=REF,
                uploader_eid=OWNER,
            )
        )
        db.commit()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Attribution Probe/SKILL.md", b"# probe")
    tree = {f"{PATH}/artifact.zip": buf.getvalue()}
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: tree)

    yield SKILL_ID

    with SessionLocal() as db:
        db.query(RpAiSkillDownload).filter(
            RpAiSkillDownload.skill_id == SKILL_ID
        ).delete(synchronize_session=False)
        row = db.get(RpAiSkill, SKILL_ID)
        if row:
            db.delete(row)
        db.commit()


def _recorded() -> list[str | None]:
    with SessionLocal() as db:
        return [
            r.eid
            for r in db.query(RpAiSkillDownload)
            .filter(RpAiSkillDownload.skill_id == SKILL_ID)
            .order_by(RpAiSkillDownload.id)
            .all()
        ]


@pytest.fixture()
def client(monkeypatch):
    """A client with the auth gate on, so ``request.state.eid`` is populated.

    The shared conftest turns the gate off, which would leave the endpoint with no
    verified identity at all — and the verified identity taking precedence is the
    thing under test.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "download-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    return TestClient(app)


@pytest.fixture()
def anon_client():
    """Auth off — the supported mode where the query param is the only identity."""
    return TestClient(app)


def _auth(eid: str) -> dict:
    token, _ = create_token(eid)
    return {"Authorization": f"Bearer {token}"}


def test_the_signed_in_caller_is_recorded(client, probe):
    resp = client.get(f"/api/skills/{probe}/download", headers=_auth(CALLER))

    assert resp.status_code == 200
    assert _recorded() == [CALLER]


def test_a_supplied_eid_cannot_override_the_signed_in_caller(client, probe):
    """The whole reason identity moved out of the query string.

    Left as it was, anyone could file their download under a colleague's name and
    the report would repeat it as fact.
    """
    resp = client.get(
        f"/api/skills/{probe}/download?eid={VICTIM}", headers=_auth(CALLER)
    )

    assert resp.status_code == 200
    assert _recorded() == [CALLER]


def test_the_query_param_still_works_when_there_is_no_auth(anon_client, probe):
    """Dev and scripted use: auth off, so the param is the only identity there is.

    Kept deliberately rather than removed — dropping it would turn every
    unauthenticated download into another unattributable row, which is the thing
    being fixed.
    """
    resp = anon_client.get(f"/api/skills/{probe}/download?eid={CALLER}")

    assert resp.status_code == 200
    assert _recorded() == [CALLER]


def test_no_identity_at_all_still_serves_the_file(anon_client, probe):
    """An anonymous download is recorded with no EID rather than refused.

    Recording is a side effect of what the user asked for; it must not be the
    reason the download fails. Such a row is what the report counts as
    unattributed, and the report says so.
    """
    resp = anon_client.get(f"/api/skills/{probe}/download")

    assert resp.status_code == 200
    assert _recorded() == [None]
