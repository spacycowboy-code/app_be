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
def _never_reach_real_gitlab(monkeypatch):
    """测试不许连真的 GitLab。

    起因是生产技能仓里积了 66 个空壳复核 MR:三个测试固件名各 22 次。机制是两件事叠起来 ——
    这里原来不清 `gitlab_token`,而有几条测试没给 `gitlab_mr.review_suspect` 打桩;于是谁的机器上
    有真令牌(比如开发机的 `.env`),跑一次测试套件就往**生产**技能仓开 3 个真 MR,并给默认复核人
    发 3 条通知。实测 GitLab 允许用不存在的源分支开 MR(201),所以连"写没成功"都挡不住它。

    代价在内容改成按引用读写之后更高:以前测试真走到写会因为没有工作副本而失败,现在 `publish`
    直接调 API,**会真的往生产仓写内容**。8-18 那次被人手工 revert 的 `audit-smoke` 上传就是同一类。

    所以这里做两件事,而不是逐处打桩(漏一处就复发):
    1. 把令牌清空、项目名指到一个明显假的值;
    2. 把出网的那两个口(仓储层与 MR 客户端)换掉,任何尝试直接让测试失败。
    """
    from app.config import get_settings
    from app.repositories import skill_content
    from app.services import gitlab_mr

    settings = get_settings()
    monkeypatch.setattr(settings, "gitlab_token", "", raising=False)
    monkeypatch.setattr(settings, "audit_mr_project", "tests/not-a-real-project", raising=False)

    def refuse(*a, **kw):
        # 用 pytest.fail 而不是 assert:它抛的是 BaseException 的子类,`except Exception` 抓不到。
        # 这一点是实测出来的 —— `gitlab_mr.review_suspect` 用一个兜底的 `except Exception` 吞掉所有
        # 异常并返回 None,所以第一版守卫抛 AssertionError 时被它吃掉,那条故意漏打桩的测试照样通过。
        pytest.fail("测试试图连真的 GitLab。要么给这一处打桩,要么说明为什么它必须出网。")

    # 守卫放在真正的出网口(`_opener`),不放在 `_fetch` / `_post` / `_request` 上:
    # 有两条测试是**故意**验证 `_fetch` 怎么处理 404 与 502 的,它们自己换掉 `_opener`。
    # 把上层函数整个换掉,那两条就没法测了;换最底层则它们自然覆盖掉这道守卫。
    monkeypatch.setattr(skill_content, "_opener", refuse)
    monkeypatch.setattr(gitlab_mr, "_opener", refuse)


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

@pytest.fixture(autouse=True)
def _audit_vocabulary(monkeypatch):
    """把引擎的分类表预置进缓存,别让测试去连网络。

    归属(谁能补上这个覆盖缺口)现在来自引擎的 GET /v1/vocabulary,门户不留副本。测试里没有引擎,
    所以这里直接把缓存填好 —— 值与引擎 skill_audit/verdict.py 里那张表一致。要验"取不到分类表"时的
    行为,单条测试把 _vocab 设成 None 即可(见 test_audit_engine_client.py)。
    """
    from app.services import audit_engine
    monkeypatch.setattr(audit_engine, "_vocab", {
        "apiVersion": audit_engine.API_VERSION,
        "causeOwners": {
            "bundle_unloadable": "author",
            "bundle_unreadable_member": "author",
            "bundle_too_big": "shared",
            "scanner_timeout": "platform",
            "scanner_broken": "platform",
            "layer_failed": "platform",
        },
    })
    monkeypatch.setattr(audit_engine, "_vocab_at", float("inf"))
