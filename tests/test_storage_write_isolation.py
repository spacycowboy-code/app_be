"""写入技能仓时,提交范围和推送目标都必须是显式的。

两条都不是风格问题,各自对应一个真实后果:

* `commit` 不带 pathspec → 提交的是整个索引。另一个并发写入刚 stage 的文件会被卷进这次提交,
  而它自己的提交随后发现"nothing to commit"。
* `push HEAD:<分支>` → HEAD 是共享可变状态。可疑内容那条路会在整个写入期间 checkout 到复核分支,
  这期间一个并发的正常发布推 `HEAD:main`,就会把**未经复核的内容**推上 live —— 审计闸门存在的
  全部意义就是拦住这个。
"""

import pytest

from app.services.audit_engine import AuditResult
from app.services.skill_storage import SkillStorage


@pytest.fixture()
def recorded(tmp_path, monkeypatch):
    """在临时目录上跑 store(),把每条 git 命令记下来而不真的执行。"""
    storage = SkillStorage()
    monkeypatch.setattr(storage, "repo", tmp_path)
    calls: list[tuple[str, ...]] = []

    class _Result:
        stdout = "0123456789abcdef"
        stderr = ""
        returncode = 0

    def fake_git(*args: str, check: bool = True):
        calls.append(args)
        return _Result()

    monkeypatch.setattr(storage, "_git", fake_git)
    monkeypatch.setattr("app.services.skill_storage.settings.skill_repo_push", True, raising=False)
    return storage, calls


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


def test_commit_is_limited_to_this_skill(recorded):
    storage, calls = recorded
    result = _store(storage)
    commits = [c for c in calls if c and c[0] == "commit"]
    assert commits, "没有产生提交"
    args = commits[0]
    assert "--" in args, f"commit 没带 pathspec,会提交整个索引: {args}"
    assert args[args.index("--") + 1] == result.storage_path


def test_push_names_the_branch_not_head(recorded):
    storage, calls = recorded
    _store(storage)
    pushes = [c for c in calls if c and c[0] == "push"]
    assert pushes, "没有推送"
    refspecs = [a for c in pushes for a in c if ":" in a]
    assert refspecs, f"推送没有 refspec: {pushes}"
    for spec in refspecs:
        assert not spec.startswith("HEAD:"), (
            f"用 HEAD 推送:并发写入切走分支时会把内容推到错误的分支 ({spec})"
        )
        assert spec.startswith("refs/heads/"), f"refspec 应显式命名本地分支: {spec}"


def test_review_branch_push_targets_that_branch(recorded):
    """可疑内容必须推到自己的复核分支,不能落到 live。"""
    storage, calls = recorded
    _store(
        storage,
        audit=AuditResult(state="suspect", verdict="suspect"),
        target_branch="review/s1-abc",
    )
    pushes = [c for c in calls if c and c[0] == "push"]
    refspecs = [a for c in pushes for a in c if ":" in a]
    assert "refs/heads/review/s1-abc:review/s1-abc" in refspecs, refspecs
    assert not any(s.endswith(":main") for s in refspecs), f"可疑内容被推向 live: {refspecs}"
