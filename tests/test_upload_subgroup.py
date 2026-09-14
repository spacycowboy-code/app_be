"""上传的包按分类落到 GitLab 子组(`<父组>/<分类 slug>`),子组不存在就回落父组。

技能侧早就按分类分目录(`skill_storage.py` 的 `rel = f"{category_slug}/..."`),tool/agent
这边此前所有人的包平铺在同一个组里 —— 「这个包属于哪个组织」在 GitLab 上看不出来。

这一组钉的重点是**回落**而不是「能拼出子组路径」:拼路径写错了会立刻被人看见,而
「子组还没建 -> 上传失败」是一个上传者无从自救、也读不懂的失败。所以第二条断言是这一组
存在的理由。
"""
from __future__ import annotations

import urllib.parse
from types import SimpleNamespace

from app.services import gitlab_projects as glw


def _settings(monkeypatch, *, agents_token: str = "a") -> None:
    monkeypatch.setattr(
        glw, "get_settings",
        lambda: SimpleNamespace(
            gitlab_user_agents_group="g/agents", gitlab_user_agents_token=agents_token,
            gitlab_user_tools_group="g/tools", gitlab_user_tools_token="t",
        ),
    )
    monkeypatch.setattr(glw, "_group_paths", {})


def test_existing_subgroup_is_used(monkeypatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(
        glw, "_api", lambda m, p, payload=None, *, target: {"id": 7},
    )
    t = glw.subgroup_target(glw.target_for("agent"), "radio-sw")
    assert t.group_path == "g/agents/radio-sw"
    # 凭据跟着父组走 —— 子组不是另一个 token。
    assert t.token == "a"
    assert t.env_var == "GITLAB_USER_AGENTS_TOKEN"


def test_missing_subgroup_falls_back_to_parent(monkeypatch) -> None:
    """子组没建时回落父组,**不抛**。

    这是这一组的主断言:让上传因为「管理员还没建那个子组」而失败,对上传者毫无意义。
    """
    _settings(monkeypatch)

    def boom(method, path, payload=None, *, target):
        raise glw.GitLabWriteError("404", status=404)

    monkeypatch.setattr(glw, "_api", boom)
    t = glw.subgroup_target(glw.target_for("agent"), "radio-sw")
    assert t.group_path == "g/agents"


def test_blank_slug_does_not_probe(monkeypatch) -> None:
    """没有分类就不该发请求 —— 一次白跑的往返,失败时还会变成一次假的回落。"""
    _settings(monkeypatch)
    calls: list[str] = []

    def spy(method, path, payload=None, *, target):
        calls.append(path)
        return {"id": 7}

    monkeypatch.setattr(glw, "_api", spy)
    for slug in (None, "", "  ", "/"):
        assert glw.subgroup_target(glw.target_for("agent"), slug).group_path == "g/agents"
    assert calls == []


def test_probe_is_cached_per_parent_and_slug(monkeypatch) -> None:
    """判定结果按 (父组, slug) 缓存:同一次进程里不该为每个包各探一次。

    按两者分键而不是只按 slug:两个上传组各有自己的子组,共用一个键会让 tool 的包按 agent
    组的探测结果落位 —— 门户不报错,只有去 GitLab 看才发现。
    """
    _settings(monkeypatch)
    asked: list[str] = []

    def spy(method, path, payload=None, *, target):
        # 探测用的是**父组的 token**,候选子组路径在 URL 里 —— 所以这里看 path 而不是
        # target.group_path。
        asked.append(urllib.parse.unquote(path))
        return {"id": 7}

    monkeypatch.setattr(glw, "_api", spy)
    assert glw.subgroup_target(glw.target_for("agent"), "ivc").group_path == "g/agents/ivc"
    assert glw.subgroup_target(glw.target_for("agent"), "ivc").group_path == "g/agents/ivc"
    assert glw.subgroup_target(glw.target_for("tool"), "ivc").group_path == "g/tools/ivc"
    # agent 探一次、tool 探一次;第二次 agent 命中缓存。
    assert asked == ["groups/g/agents/ivc", "groups/g/tools/ivc"]


def test_missing_token_returns_parent_without_probing(monkeypatch) -> None:
    """凭据没配时原样返回,不在这里抢先抛。

    真正那句「哪一个变量没配」的消息在 `create_project` 那步给。在这里抛一个措辞不同的错
    会让人去查错的地方 —— 而这两条路径的修法是同一件事。
    """
    _settings(monkeypatch, agents_token="")
    calls: list[str] = []

    def spy(method, path, payload=None, *, target):
        calls.append(path)
        return {"id": 7}

    monkeypatch.setattr(glw, "_api", spy)
    t = glw.subgroup_target(glw.target_for("agent"), "radio-sw")
    assert t.group_path == "g/agents"
    assert calls == []


def test_namespace_id_resolves_the_subgroup_path(monkeypatch) -> None:
    """收窄后的 target 拿到的是**子组**的 id。

    `namespace_id` 的缓存本来就按路径分键,所以这里不需要新缓存;这条钉住的是「收窄没有
    白做」—— 少了它,group_path 变了而建项目仍然用父组 id,包照样堆在父组、且完全静默。
    """
    _settings(monkeypatch)
    monkeypatch.setattr(glw, "_namespace_ids", {})

    def fake_api(method, path, payload=None, *, target):
        return {"id": 99 if target.group_path.endswith("/radio-sw") else 11}

    monkeypatch.setattr(glw, "_api", fake_api)
    parent = glw.target_for("agent")
    assert glw.namespace_id(parent) == 11
    assert glw.namespace_id(glw.subgroup_target(parent, "radio-sw")) == 99
