"""从 Gerrit 同步 ``radio/ai-assets``。

Gerrit 与 LDAP 都被替换掉:一个需要凭据才能连的测试等于没有测试,它会在 CI 上静默跳过。替换点
选在两个仓库/目录边界上(``gerrit_content`` 的读函数、``asset_owner._Directory.search``),所以
被测的是这个功能自己的全部判断 —— 扫哪些目录、幂等键怎么拼、owner 解析链的顺序、字段怎么映射、
上游少了东西怎么办。

夹具里的资产是从真实的 19 个里挑的,每一个都对应一种当时踩过或拍过的情况。
"""
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.repositories import gerrit_content
from app.services import asset_owner, gerrit_assets
from app.services.dept_groups import DEPT_GROUP_MAP, ensure_group_categories

HEAD = "0027853a48e683e7f2abf01b3c02f320541e06c9"

#: 目录名 -> (tree sha, {文件名: 内容})。sha 是变更判据,所以测试要能改它。
TREE: dict[str, dict] = {
    "skill": {
        "INDEX.md": ("blob", "sha-index", "generated"),
        "README.md": ("blob", "sha-readme", "hi"),
        # 纯工号 owner,部门在 BNEW RTE RP SWD -> radio-sw
        "ai-coding-collab": ("tree", "sha-acc", {
            "manifest.yaml": (
                'name: ai-coding-collab\n'
                'version: "1.0.0"\n'
                'description: Human-AI pair programming workflow\n'
                'when_to_use: When starting a coding task with AI\n'
                'capabilities:\n'
                '  - Guide collaboration through phases\n'
                '  - Prevent scope creep\n'
                'owner: ecuiden\n'
                'type: prompt\n'
            ),
            "SKILL.md": "# ai-coding-collab\n正文\n",
        }),
        # owner 在 BNEW RTE RA(另一个组织)-> category_id 必须留空
        "commit-message-creator": ("tree", "sha-cmc", {
            "manifest.yaml": (
                'name: commit-message-creator\n'
                'version: "1.0.0"\n'
                'description: Writes Gerrit commit messages.\n'
                'owner: Adam Eriksson (eeriada)\n'
            ),
            "SKILL.md": "# commit-message-creator\n",
        }),
        # owner 是团队名,目录里查不到 -> 兜底
        "index-generator": ("tree", "sha-ig", {
            "manifest.yaml": (
                'name: index-generator\n'
                'description: Generates INDEX.md\n'
                'owner: radio-ai-platform\n'
                'type: script\n'
            ),
            "SKILL.md": "# index-generator\n",
        }),
        # 没有 manifest:不是资产,跳过
        "not-an-asset": ("tree", "sha-naa", {"README.md": "just docs"}),
    },
    "agent": {
        "RULES.md": ("blob", "sha-rules", "rules"),
        # 逗号分隔的两个人 -> 只取第一个
        "swat-bot": ("tree", "sha-swat", {
            "manifest.yaml": (
                'name: swat-bot\n'
                'description: SWAT triage bot.\n'
                'owner: Dilan Ismail, Theo Friberg\n'
            ),
            "README.md": "# swat-bot\n",
        }),
    },
    "tool": {
        # firstname.lastname 形式的 owner
        "kiro-jira": ("tree", "sha-kj", {
            "manifest.yaml": (
                'name: kiro-jira\n'
                'version: "1.0.0"\n'
                'description: Create and sync Jira tickets.\n'
                'owner: shaolei.zhang\n'
                'type: cli-tool\n'
            ),
            "install.sh": "#!/bin/sh\n",
        }),
    },
}

#: 替代 LDAP。键是过滤器里被查的那个值,值是 (signum, displayName, department)。
DIRECTORY = {
    "ecuiden": ("ecuiden", "Tracy Deng", "BNEW RTE RP SWD SW2 Software 2"),
    "eeriada": ("eeriada", "Adam Eriksson", "BNEW RTE RA RD5 SWD Software Design D"),
    "Dilan Ismail": ("eismdil", "Dilan Ismail", "BNEW RTE RA RD5 SWA Software Design A"),
    "shaolei.zhang": ("ehshaza", "Shaolei Zhang", "BNEW RTE RP SWD SW3 Software 3"),
}


def _fake_search(flt: str):
    """按过滤器里出现的值查夹具,模拟目录的行为(查不到就空)。"""
    for key, row in DIRECTORY.items():
        # 过滤器形如 (sAMAccountName=ecuiden) / (displayName=Dilan Ismail) / (mail=shaolei.zhang@*)
        if f"={key})" in flt or f"={key}@*)" in flt or f"={key}*)" in flt:
            return [row]
    return []


@pytest.fixture
def gerrit(monkeypatch):
    def head_ref(branch=None):
        return HEAD

    def list_dir(ref, path):
        assert ref == HEAD, "所有读都必须钉在同一个引用上"
        node = TREE
        parts = [p for p in path.strip("/").split("/") if p]
        for part in parts:
            entry = node.get(part)
            if entry is None:
                raise gerrit_content.NotFound(path)
            node = entry[2] if isinstance(entry, tuple) else entry
        out = []
        for name, entry in node.items():
            if isinstance(entry, tuple):
                out.append({"name": name, "type": entry[0], "id": entry[1]})
            else:
                out.append({"name": name, "type": "tree", "id": f"sha-{name}"})
        return out

    def _blob(ref, path):
        node = TREE
        parts = [p for p in path.strip("/").split("/") if p]
        for part in parts[:-1]:
            entry = node.get(part)
            if entry is None:
                raise gerrit_content.NotFound(path)
            node = entry[2] if isinstance(entry, tuple) else entry
        leaf = node.get(parts[-1])
        if leaf is None:
            raise gerrit_content.NotFound(path)
        body = leaf[2] if isinstance(leaf, tuple) else leaf
        if not isinstance(body, str):
            raise gerrit_content.NotFound(path)
        return body

    monkeypatch.setattr(gerrit_content, "head_ref", head_ref)
    monkeypatch.setattr(gerrit_content, "list_dir", list_dir)
    monkeypatch.setattr(gerrit_content, "read_text", _blob)
    monkeypatch.setattr(
        gerrit_content, "read", lambda ref, path: _blob(ref, path).encode()
    )
    monkeypatch.setattr(asset_owner._Directory, "search", lambda self, flt: _fake_search(flt))
    monkeypatch.setattr(asset_owner._Directory, "close", lambda self: None)
    monkeypatch.setenv("GERRIT_OWNER_FALLBACK_EID", "ECUIDEN")
    get_settings.cache_clear()
    with SessionLocal() as db:
        # 和容器启动时一样建齐所有分组行,而不是手挑两个。这样夹具跟生产一致:
        # 部门映射不到分组的作者(这个仓里 ``BNEW RTE RA`` 的那些)会落到 ``others``,
        # 而 ``others`` 也是启动时建出来的 —— ``category_id`` 是外键,少了那一行 INSERT 就挂。
        ensure_group_categories(db)
        for slug, label in [("radio-sw", "Radio SW"), ("ai-team", "AI Team")]:
            if db.get(RpAiCategory, slug) is None:
                db.add(RpAiCategory(id=slug, label=label))
        db.commit()
    yield
    get_settings.cache_clear()


def _run() -> dict:
    with SessionLocal() as db:
        return gerrit_assets.sync_ai_assets(db)


def _by_dir(report: dict) -> dict[str, dict]:
    return {f"{a['kind']}/{a['directory']}": a for a in report["assets"]}


# --- 扫哪些东西 -------------------------------------------------------------


def test_the_three_asset_directories_are_scanned_and_docs_are_not(gerrit):
    """``INDEX.md`` / ``README.md`` / ``RULES.md`` 是说明文件,不是资产。"""
    assets = _by_dir(_run())
    assert set(assets) == {
        "skill/ai-coding-collab",
        "skill/commit-message-creator",
        "skill/index-generator",
        "skill/not-an-asset",
        "agent/swat-bot",
        "tool/kiro-jira",
    }


def test_a_directory_without_a_manifest_is_skipped_not_guessed(gerrit):
    """没有 manifest 就没有 name / owner / description,猜出来的资产比没有更糟。"""
    entry = _by_dir(_run())["skill/not-an-asset"]
    assert entry["action"] == "skipped"
    assert "manifest" in entry["reason"]
    with SessionLocal() as db:
        assert db.get(RpAiSkill, "gerrit-skill-not-an-asset") is None


def test_skills_and_market_items_land_in_their_own_tables(gerrit):
    _run()
    with SessionLocal() as db:
        assert db.get(RpAiSkill, "gerrit-skill-ai-coding-collab") is not None
        assert db.get(RpAiMarketItem, "gerrit-agent-swat-bot") is not None
        assert db.get(RpAiMarketItem, "gerrit-tool-kiro-jira") is not None
        # skill 不进市场表,agent 不进技能表
        assert db.get(RpAiMarketItem, "gerrit-skill-ai-coding-collab") is None
        assert db.get(RpAiSkill, "gerrit-agent-swat-bot") is None


# --- 幂等 -------------------------------------------------------------------


def test_running_twice_changes_nothing(gerrit):
    """第二次全是 unchanged —— 幂等键认得出上一次的行。"""
    assert set(_run()["counts"]) == {"created", "skipped"}
    second = _run()
    assert second["counts"].get("created") is None
    assert second["counts"]["unchanged"] == 5


def test_a_changed_directory_sha_is_what_triggers_an_update(gerrit):
    """变更判据是目录的 tree sha,不是版本号:一半资产的 manifest 没有版本号。"""
    _run()
    original = TREE["skill"]["ai-coding-collab"]
    TREE["skill"]["ai-coding-collab"] = ("tree", "sha-acc-v2", original[2])
    try:
        entry = _by_dir(_run())["skill/ai-coding-collab"]
        assert entry["action"] == "updated"
    finally:
        TREE["skill"]["ai-coding-collab"] = original


def test_the_commit_sha_is_stored_for_reads_and_the_tree_sha_for_change_detection(gerrit):
    """两个 sha 不能混。

    第一版把目录的 tree sha 写进了 ``repo_commit``,读正文时 Gitiles 直接 404 —— 一个 tree
    sha 不是路径能解析的东西。这条钉住那个区分。
    """
    _run()
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, "gerrit-skill-ai-coding-collab")
        assert skill.repo_commit == HEAD, "读路径要用提交 sha"
        assert skill.content_sha == "sha-acc", "变更判据要用目录 sha"
        item = db.get(RpAiMarketItem, "gerrit-tool-kiro-jira")
        assert item.content_sha == "sha-kj"


# --- owner 解析 -------------------------------------------------------------


def test_a_signum_owner_is_used_directly(gerrit):
    entry = _by_dir(_run())["skill/ai-coding-collab"]
    assert entry["creator"] == "ECUIDEN"
    assert entry["author"] == "Tracy Deng"
    assert entry["ownerResolvedBy"] == "当工号"


def test_a_signum_in_parentheses_wins_over_the_name(gerrit):
    """``Adam Eriksson (eeriada)`` 按显示名是查不到的:目录里的显示名不带括号。"""
    entry = _by_dir(_run())["skill/commit-message-creator"]
    assert entry["creator"] == "EERIADA"
    assert entry["ownerResolvedBy"] == "括号里的工号"


def test_a_firstname_lastname_owner_is_resolved_through_mail(gerrit):
    entry = _by_dir(_run())["tool/kiro-jira"]
    assert entry["creator"] == "EHSHAZA"
    assert entry["author"] == "Shaolei Zhang"


def test_two_owners_credit_the_first_one(gerrit):
    """``creator`` 只能是一个工号,``author`` 跟着它 —— 第二个人在门户里不出现。"""
    entry = _by_dir(_run())["agent/swat-bot"]
    assert entry["creator"] == "EISMDIL"
    assert entry["author"] == "Dilan Ismail"


def test_an_owner_the_directory_does_not_know_falls_back_and_says_so(gerrit):
    """兜底必须是可见的:报表会把这个资产算成兜底那个人的产出。"""
    report = _run()
    entry = _by_dir(report)["skill/index-generator"]
    assert entry["creator"] == "ECUIDEN"
    assert "查不到" in entry["ownerResolvedBy"]
    assert [f["asset"] for f in report["fellBackOwners"]] == ["skill/index-generator"]


# --- 归属 -------------------------------------------------------------------


def test_an_owner_inside_the_reported_organisation_gets_a_department(gerrit):
    assert _by_dir(_run())["skill/ai-coding-collab"]["categoryId"] == "radio-sw"


def test_an_owner_outside_BNEW_RTE_RP_goes_to_others(gerrit):
    """这个仓相当一部分作者在 ``BNEW RTE RA``(``RD5`` 是活的案例)。

    以前这里断言的是"留空"。留空有个只在数字上露出来的代价:资产照样计入市场的 "All",却
    不出现在任何部门标签下,于是标签加起来不等于总数 —— 而做这件事的行恰好在唯一能暴露它的
    视图里看不见。线上这么漂出去 10 条才被发现。

    现在归到 ``others``。这条测试原本真正要防的东西一点没松:**不能被塞进某个 RP 的分组**,
    那才是编造归属。``others`` 不声称任何部门,所以两件事可以同时成立 —— 下面两个断言各钉
    一件。
    """
    cid = _by_dir(_run())["skill/commit-message-creator"]["categoryId"]
    assert cid == "others"
    assert cid not in set(DEPT_GROUP_MAP.values()), "RA 的人不能落进任何一个 RP 部门的分组"


def test_a_creator_who_never_signed_in_gets_an_import_row(gerrit):
    """没有账号行的人,报表认不出他、卡片也没有组徽标。

    ``origin='import'`` 才不会让"曾登录过的人数"把他们算进去。
    """
    _run()
    with SessionLocal() as db:
        row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == "EISMDIL"))
        assert row is not None
        assert row.origin == "import"
        assert row.department == "BNEW RTE RA RD5 SWA Software Design A"


def test_an_existing_portal_row_is_not_demoted_to_import(gerrit):
    """真实登录写下的行比目录快照更权威,同步不该把它改成 import。"""
    with SessionLocal() as db:
        db.add(RpAiPortal(eid="ECUIDEN", display_name="Tracy Deng", department="X"))
        db.commit()
    _run()
    with SessionLocal() as db:
        row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == "ECUIDEN"))
        assert row.origin == "login"
        assert row.department == "X", "已有部门不被覆盖"


# --- 字段映射 ---------------------------------------------------------------


def test_market_items_point_at_the_repository_and_not_at_a_service(gerrit):
    """这 11 个 agent/tool 没有一个是已经在跑的服务,填 cloud 会让"打开"指向不存在的地址。"""
    _run()
    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, "gerrit-tool-kiro-jira")
        assert item.service_type == "code"
        assert item.service_url is None
        assert item.repo_url and "tool/kiro-jira" in item.repo_url
        assert item.source == gerrit_assets.MARKET_SOURCE


def test_when_to_use_and_capabilities_are_kept(gerrit):
    """``description`` 上游自己限定 120 字,只拿它会丢掉最有用的那部分。"""
    _run()
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, "gerrit-skill-ai-coding-collab")
        assert "When starting a coding task" in skill.capability_full
        assert "Prevent scope creep" in skill.capability_full


def test_the_upstream_type_becomes_a_tag(gerrit):
    _run()
    with SessionLocal() as db:
        assert db.get(RpAiSkill, "gerrit-skill-ai-coding-collab").tags == ["skill", "prompt"]
        # 没有 type 字段的资产只带 kind,不编造标签
        assert db.get(RpAiSkill, "gerrit-skill-commit-message-creator").tags == ["skill"]


def test_skill_content_is_read_by_reference_not_copied_into_the_database(gerrit):
    """``storage_path`` 带 ``gerrit:`` 前缀,读正文时据此分派到 Gerrit。"""
    _run()
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, "gerrit-skill-ai-coding-collab")
        assert skill.storage_path == "gerrit:skill/ai-coding-collab"
        from app.services import asset_content

        assert asset_content.is_gerrit(skill.storage_path)


# --- 上游少了东西 -----------------------------------------------------------


def test_an_asset_that_disappears_upstream_is_reported_not_deleted(gerrit):
    """删一条卡片会连带删掉别人的下载与收藏(三处 CASCADE),那是唯一不可回溯的动作。

    而"这次没扫到"可能只是一次网络抖动或一份写坏的 manifest。
    """
    _run()
    removed = TREE["tool"].pop("kiro-jira")
    try:
        report = _run()
        assert report["notSeenThisRun"] == ["gerrit-tool-kiro-jira"]
        with SessionLocal() as db:
            assert db.get(RpAiMarketItem, "gerrit-tool-kiro-jira") is not None
    finally:
        TREE["tool"]["kiro-jira"] = removed


# --- 门户是只读下游 ---------------------------------------------------------


@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def test_editing_a_synced_skill_is_refused(gerrit, api):
    """自审时发现的真缺陷,不是假想。

    这条路径会把 ``storage_path`` 当成"门户自己那个技能仓里的路径"交给
    ``skill_storage``,而同步来的值是 ``gerrit:skill/xxx`` —— 结果是在门户的仓里造一个名叫
    ``gerrit:skill`` 的目录。而且即使写对了也是徒劳:下一次同步会照上游覆盖回来。
    """
    _run()
    r = api.put(
        "/api/skills/gerrit-skill-ai-coding-collab",
        json={"description": "我在门户里改了描述", "editorEid": "EZHGMIO"},
    )
    assert r.status_code == 409, r.text
    assert "radio/ai-assets" in r.json()["detail"]
    with SessionLocal() as db:
        skill = db.get(RpAiSkill, "gerrit-skill-ai-coding-collab")
        assert skill.description == "Human-AI pair programming workflow"


def test_taking_a_synced_skill_off_the_market_is_refused(gerrit, api):
    """下架会对 ``gerrit:skill/...`` 这个前缀调 publish,去门户自己的仓里删一份不存在的内容。"""
    _run()
    r = api.request(
        "DELETE",
        "/api/skills/gerrit-skill-ai-coding-collab",
        headers={"X-User-Eid": "EZHGMIO"},
    )
    assert r.status_code == 409, r.text
    with SessionLocal() as db:
        assert db.get(RpAiSkill, "gerrit-skill-ai-coding-collab").status == "approved"


def test_the_guard_only_fires_for_synced_skills(gerrit):
    """守卫必须只挡同步来的那些,否则它就是个功能回归。

    直接测这个函数而不是走 API:普通技能的编辑路径要读门户自己那个内容仓,测试环境里连不上,
    那条失败与守卫无关,只会把这条测试变成噪音。
    """
    from app.routers.skills import _refuse_if_synced

    _run()
    with SessionLocal() as db:
        with pytest.raises(Exception) as exc:
            _refuse_if_synced(db.get(RpAiSkill, "gerrit-skill-ai-coding-collab"))
        assert "radio/ai-assets" in str(exc.value)

    # 不抛就是通过:普通技能照常可改。
    _refuse_if_synced(
        RpAiSkill(id="hand-made", name="Hand Made", storage_path="public/hand-made")
    )
    # 从没记过位置的老数据同样不该被挡。
    _refuse_if_synced(RpAiSkill(id="no-path", name="No Path"))
