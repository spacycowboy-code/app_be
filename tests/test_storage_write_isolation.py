"""写入技能仓时,写的范围和落在哪个分支都必须是显式的。

这四条原来盯的是 git 命令行:`commit` 要带 pathspec、`push` 不能用 `HEAD:`、可疑内容只能推到自己的
复核分支、复核分支必须从远端的 live 切。它们各自对应一个真实后果:

* 提交不带 pathspec → 提交整个索引,另一个并发写入刚 stage 的文件被卷进这次提交。
* `push HEAD:<分支>` → HEAD 是共享可变状态。可疑内容那条路整个写入期间都 checkout 在复核分支上,
  这期间一个并发的正常发布推 `HEAD:main`,**未经复核的内容就上了 live** —— 审计闸门存在的全部意义
  就是拦住这个。
* 复核分支从**本地** ref 切 → 本地 ref 会漂,切出来的分支 diff 里带着"把这期间合入的东西全 revert 掉"。

改成一次原子写之后这些性质是结构上成立的(没有索引、没有 HEAD、没有本地 ref),但性质本身还得有人盯,
所以测试搬到新的层次:断言这次写**发出去的是什么** —— 动作的路径范围、落在哪个分支、从哪切。
"""

import pytest

from app.repositories import skill_content
from app.services.audit_engine import AuditResult
from app.services.skill_storage import SkillStorage

LIVE = "main"


@pytest.fixture()
def published(monkeypatch):
    """跑 store(),把发给内容存储的那次写记下来而不真的发出去。"""
    seen: dict = {}

    def fake_publish(*, prefix, files, expect, branch, message, start_branch=None):
        seen.update(prefix=prefix, files=files, expect=expect, branch=branch,
                    message=message, start_branch=start_branch)
        return "n" * 40

    monkeypatch.setattr(skill_content, "publish", fake_publish)
    monkeypatch.setattr(skill_content, "head_ref", lambda branch=None: "h" * 40)
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: {})
    monkeypatch.setattr(skill_content, "tag", lambda **kw: True)
    monkeypatch.setattr(
        "app.services.skill_storage.settings.skill_repo_branch", LIVE, raising=False
    )
    return SkillStorage(), seen


def _store(storage, **overrides):
    kwargs = dict(
        skill_id="s1",
        eid="EUSER",
        name="Some Skill",
        category_slug="cat",
        manifest={"name": "Some Skill"},
        instructions="# hi\n",
        artifact_bytes=None,
        artifact_filename=None,
        current_version=None,
        existing_storage_path=None,
        audit=AuditResult(state="pass", verdict="pass"),
    )
    kwargs.update(overrides)
    return storage.store(**kwargs)


def test_the_write_touches_only_this_skills_folder(published):
    storage, seen = published
    result = _store(storage)
    assert seen["prefix"] == result.storage_path
    for rel in seen["files"]:
        assert not rel.startswith("/") and ".." not in rel, rel


def test_a_normal_publish_lands_on_the_live_branch(published):
    storage, seen = published
    _store(storage)
    assert seen["branch"] == LIVE
    assert seen["start_branch"] is None, "live 分支已经存在,不该要求切分支"


def test_suspect_content_lands_only_on_its_review_branch(published):
    """可疑内容必须落在自己的复核分支,不能落到 live。"""
    storage, seen = published
    _store(
        storage,
        audit=AuditResult(state="suspect", verdict="suspect"),
        target_branch="review/s1-abc",
    )
    assert seen["branch"] == "review/s1-abc"
    assert seen["branch"] != LIVE


def test_the_review_branch_is_cut_from_the_live_branch_in_the_same_write(published):
    storage, seen = published
    _store(
        storage,
        audit=AuditResult(state="suspect", verdict="suspect"),
        target_branch="review/s1-abc",
    )
    assert seen["start_branch"] == LIVE, (
        "复核分支必须由这次调用从 live 切出来 —— 本地 ref 会漂,从漂了的 ref 切出来的分支"
        "会把这期间合入的东西 revert 掉"
    )


def test_suspect_content_is_refused_when_no_review_branch_is_given(published):
    storage, _ = published
    with pytest.raises(ValueError):
        _store(storage, audit=AuditResult(state="suspect", verdict="suspect"))


def test_rejected_content_is_never_written(published):
    storage, seen = published
    with pytest.raises(ValueError):
        _store(storage, audit=AuditResult(state="danger", verdict="danger"))
    assert seen == {}, "审计拒绝的内容却发出了写请求"


def test_an_existing_skill_is_written_with_the_ref_it_was_read_at(published):
    """已存在的技能必须带前置条件:不带就是静默覆盖。"""
    storage, seen = published
    _store(storage, existing_storage_path="cat/s1_EUSER_Some_Skill")
    assert seen["expect"] == "h" * 40


def test_a_brand_new_skill_has_nothing_to_base_on(published):
    storage, seen = published
    _store(storage)
    assert seen["expect"] is None
