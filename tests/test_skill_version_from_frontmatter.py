"""发布的版本号来自作者 SKILL.md 里声明的 `version`，平台自增只是兜底。

**这个缺陷原来的形状。** 徽标上的版本号完全由 `bump_minor` 自增，与 frontmatter 里的
`version` 无关，两个数字各走各路。生产实测（2026-09-22，56 个有内容引用的已上市技能）：
**顶层**声明了 `version` 的有 5 个，没有一个与徽标一致；另有 7 个只把 `version` 写在
`metadata:` 底下（不是规范里那个顶层字段，本实现刻意不读，按「没声明」处理）；44 个完全没写。
`outlook-assistant` 那次把两个数字写进了同一行数据库记录 —— 版本号 `1.5.0`，而同一行的
`artifact_name` 是 `outlook-assistant-v1.6.0.zip`。

**为什么不能无条件采用声明值。** 两种形状必须拒：已经发布过的号会让 tag 在最后一步被拒
（内容已经进了分支、只有 tag 没打上）；比已发布的旧的号会让徽标和 tag 序列往回走。这两道守卫
**今天在生产上都不会被触发** —— 那 5 个顶层声明全部领先于已发布值 —— 但守卫仍然要在，因为
触发时的后果一个是半成品状态、一个是静默回退。它们各有一条单元测试钉着。

**同步来的技能不在这条路上**：IVC / Gerrit / official 三条同步路径直接沿用上游版本号，
从来不走 `bump_minor`，本次一个字没动它们。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models.skill import RpAiSkill
from app.models.version import RpAiSkillVersion
from app.repositories import skill_content
from app.routers import upload as upload_mod
from app.services.audit_engine import AuditResult
from app.services.skill_manifest import frontmatter_version
from app.services.skill_storage import plan_version, skill_storage, version_key


def _md(version_line: str = "", *, name: str = "T") -> str:
    """一份最小 SKILL.md；`version_line` 原样插进 frontmatter。"""
    return f"---\nname: {name}\n{version_line}description: d\n---\n\n# {name}\nbody\n"


# ---------------------------------------------------------------- 读文件那一层


def test_reads_the_declared_version():
    assert frontmatter_version(_md("version: 1.6.0\n")) == "1.6.0"


def test_a_missing_version_is_not_an_error():
    """44 个生产技能没声明版本号，这是常态而不是异常。"""
    assert frontmatter_version(_md()) is None


def test_quoted_and_unquoted_read_the_same():
    assert frontmatter_version(_md('version: "1.6.0"\n')) == "1.6.0"
    assert frontmatter_version(_md("version: '1.6.0'\n")) == "1.6.0"


def test_yaml_turns_a_two_part_version_into_a_number_and_we_keep_it_readable():
    """`version: 1.6` 被 YAML 读成 float —— 要发出 `1.6` 而不是 `1.6` 的 repr。"""
    assert frontmatter_version(_md("version: 1.6\n")) == "1.6"


def test_a_non_semver_scheme_is_passed_through():
    """生产里 system-ds-data-analyzer 用日期做版本号，不能被改写成 semver。"""
    assert frontmatter_version(_md("version: 2026.09.09\n")) == "2026.09.09"


def test_no_frontmatter_block_falls_back_to_the_inline_value():
    """与 name / description 同样的兜底：没有 `---` 块时按行取值。"""
    assert frontmatter_version("name: T\nversion: 3.1.0\ndescription: d\n") == "3.1.0"


def test_nothing_in_nothing_out():
    assert frontmatter_version("") is None
    assert frontmatter_version("# 没有 frontmatter 的正文\n") is None


def test_a_version_nested_under_another_key_is_not_a_declared_version():
    """嵌套的 `version` 不算声明 —— 这一条钉住那个「差点数错」的形状。

    生产里 7 个技能把版本号写在 `metadata:` 底下（`plda-test-report-bundle` 的 0.5.0、
    `system-ds-data-analyzer` 的 2026.09.09 等）。规范里 `version` 是顶层字段，猜作者用了
    哪一层嵌套是会静默出错的预测；而且一旦读了它们，其中几个比已发布值小，会让页面版本号往回
    跳。所以一律按「没声明」处理、继续自增。

    第一版统计用的是对 frontmatter 块的粗正则，把这 7 个也算成了声明值，于是「声明了版本号的
    12 个全部不一致」这个数字是错的；真实数字是顶层声明 5 个、其中 0 个一致。
    """
    nested = (
        "---\n"
        "name: T\n"
        "description: d\n"
        "metadata:\n"
        "  version: 0.5.0\n"
        "---\n\n"
        "# T\n"
    )
    assert frontmatter_version(nested) is None
    # 顶层那个照常读到 —— 证明上面的 None 不是「整个块都没解析成功」。
    top = nested.replace("metadata:\n  version: 0.5.0", "version: 0.5.0")
    assert frontmatter_version(top) == "0.5.0"


# ------------------------------------------------------------------ 判断那一层


def test_a_newer_declared_version_is_published_as_written():
    plan = plan_version(declared="1.6.0", current="1.5.0", taken={"1.5.0"})
    assert (plan.version, plan.source) == ("1.6.0", "declared")
    assert plan.note == ""          # 直接采用，没什么要解释的


def test_a_declared_version_that_was_already_published_falls_back():
    """tag 是 `<id>@<version>`：重复的号会让内容进了分支而 tag 没打上。"""
    plan = plan_version(declared="1.5.0", current="1.5.0", taken={"1.4.0", "1.5.0"})
    assert (plan.version, plan.source) == ("1.6.0", "auto")
    assert "1.5.0" in plan.note and "1.6.0" in plan.note


def test_a_declared_version_older_than_what_is_published_falls_back():
    """今天生产上没有技能触发这条，但触发时的后果是徽标和 tag 序列静默回退。

    之所以一度以为 `plda-test-report-bundle`（已发到 1.7.0）声明了 0.5.0：那个 0.5.0 写在
    `metadata:` 底下，不是顶层 `version`，`frontmatter_version` 正确地读不到它。
    """
    plan = plan_version(declared="0.5.0", current="1.7.0", taken={"1.7.0"})
    assert (plan.version, plan.source) == ("1.8.0", "auto")
    assert "0.5.0" in plan.note and "1.7.0" in plan.note


def test_an_equal_declared_version_is_not_newer():
    """边界：相等不算更新，否则重传同一个号会走到撞号那条路上去。"""
    assert plan_version(declared="1.5.0", current="1.5.0").source == "auto"


def test_no_declared_version_keeps_the_old_behaviour_silently():
    """那 44 个技能的行为必须一个字不变，而且不能收到一句解释。"""
    plan = plan_version(declared=None, current="1.5.0")
    assert (plan.version, plan.source, plan.note) == ("1.6.0", "auto", "")


def test_first_upload_takes_the_declared_version():
    """还没有任何已发布版本时，声明值没有可比的对象，直接成立 —— 包括 2.0.0。"""
    plan = plan_version(declared="2.0.0", current=None)
    assert (plan.version, plan.source) == ("2.0.0", "declared")


def test_first_upload_without_a_declared_version_is_still_1_0_0():
    assert plan_version(declared=None, current=None).version == "1.0.0"


def test_bump_minor_could_never_have_expressed_a_major_release():
    """这条钉住动机：作者要发 2.0.0，自增只会答 1.1.0。"""
    assert plan_version(declared=None, current="1.0.0").version == "1.1.0"
    assert plan_version(declared="2.0.0", current="1.0.0").version == "2.0.0"


def test_version_key_orders_by_digits_in_order():
    assert version_key("1.7.0") > version_key("0.5.0")
    assert version_key("2026.09.09") > version_key("1.1.0")
    assert version_key(None) == (0, 0, 0)
    assert version_key("abc") == (0, 0, 0)


# -------------------------------------------------------------------- 接线那一层


@pytest.fixture()
def repo(monkeypatch):
    """把内容仓换成内存里的一棵树，让真的 `store()` 跑起来。

    形状照 tests/test_body_has_one_source.py。返回 publish 收到的文件字典，
    以便断言写进 manifest.json 的版本号。
    """
    written: dict = {}

    def publish(**kw):
        written.update(kw.get("files") or {})
        return "n" * 40

    monkeypatch.setattr(skill_content, "head_ref", lambda branch=None: "h" * 40)
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: {})
    monkeypatch.setattr(skill_content, "publish", publish)
    monkeypatch.setattr(skill_content, "tag", lambda **kw: True)
    return written


def test_store_publishes_the_declared_version(repo):
    """接线测试：判断函数写对了但没接上 `store()`，等于什么都没做。"""
    result = skill_storage.store(
        skill_id="wire-1",
        eid="EZHGMIO",
        name="T",
        category_slug="public",
        manifest={},
        instructions=_md("version: 4.2.0\n"),
        artifact_bytes=None,
        artifact_filename=None,
        current_version="1.0.0",
        existing_storage_path=None,
        audit=AuditResult(state="pass"),
    )
    assert result.version == "4.2.0"
    assert result.version_source == "declared"
    assert b"4.2.0" in repo["manifest.json"]


def test_store_honours_taken_and_reports_why(repo):
    """`taken` 真的被用上了：声明值撞号时回落，并带一句给作者的解释。"""
    result = skill_storage.store(
        skill_id="wire-2",
        eid="EZHGMIO",
        name="T",
        category_slug="public",
        manifest={},
        instructions=_md("version: 2.0.0\n"),
        artifact_bytes=None,
        artifact_filename=None,
        current_version="1.0.0",
        existing_storage_path=None,
        audit=AuditResult(state="pass"),
        taken={"2.0.0"},
    )
    assert result.version == "1.1.0"
    assert result.version_source == "auto"
    assert "2.0.0" in result.version_note


# --------------------------------------------------------------- 发布前的预告


@pytest.fixture()
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False, raising=False)
    monkeypatch.setattr(upload_mod, "audit_skill", lambda **kw: AuditResult(state="pass"))
    return TestClient(app)


def _evaluate(client, md: str, *, skill_id: str | None = None):
    data = {"eid": "EZHGMIO", "name": "T", "instructions": md}
    if skill_id is not None:
        # 字段名与 /api/skills/upload 一致（那边也是 `id`），向导两步传同一个名字。
        data["id"] = skill_id
    return client.post("/api/skills/evaluate", data=data)


def test_evaluate_previews_the_version_before_publishing(client):
    """作者在点发布**之前**就该看到会发哪个号 —— 这是「不误导人」的那一半。"""
    r = _evaluate(client, _md("version: 9.9.0\n"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plannedVersion"] == "9.9.0"
    assert body["versionNote"] == ""


def test_evaluate_explains_a_version_it_will_not_use(client):
    """已发布过 1.5.0 的技能再声明 1.5.0：预告里要说清跳过了哪个值。"""
    with SessionLocal() as db:
        db.add(RpAiSkill(id="prev-1", name="T", version="1.5.0", status="approved"))
        db.add(RpAiSkillVersion(skill_id="prev-1", version="1.5.0"))
        db.commit()

    body = _evaluate(client, _md("version: 1.5.0\n"), skill_id="prev-1").json()
    assert body["plannedVersion"] == "1.6.0"
    assert "1.5.0" in body["versionNote"]


def test_evaluate_stays_quiet_when_there_is_nothing_to_explain(client):
    """没声明版本号的作者不该收到一句关于他没碰过的机制的说明。"""
    body = _evaluate(client, _md()).json()
    assert body["versionNote"] == ""
    assert body["plannedVersion"] == "1.0.0"
