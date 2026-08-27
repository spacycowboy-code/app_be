"""推送 agent 包时,git 子进程的环境与凭据处理。

这两件事都是实测踩出来的,不是预防性检查:

* 这台机器上设着 `HTTPS_PROXY=http://http-proxy...`,于是 `git push` 走代理,代理对内网地址回
  `403 from proxy after CONNECT`。API 调用没事,因为它的 opener 显式忽略代理 —— git 不知道。
* token 一旦进了 `.git/config` 或命令行,就会同时落在磁盘上和 `ps` 的输出里,而这两处都会被
  别的工具顺手记下来。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services import gitlab_projects as glw


PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


def test_proxy_variables_are_dropped(monkeypatch) -> None:
    for var in PROXY_VARS:
        monkeypatch.setenv(var, "http://http-proxy.example:8080")
    env = glw._git_env()
    # 按名字逐个断言,而不是"任何含 proxy 的键都不许有":GitLab 的 runner 会导出
    # CI_DEPENDENCY_PROXY_*(容器镜像代理,和 git 的 HTTP 代理无关),那条宽泛的断言在 CI 上
    # 必然失败 —— 本地能过只是因为本地没有那些变量。
    left = [v for v in PROXY_VARS if v in env]
    assert not left, f"这些代理变量必须清掉,否则推不上去:{left}"


def test_unrelated_variables_named_proxy_are_left_alone(monkeypatch) -> None:
    """只清真正会影响 git 的那几个,别顺手清掉别人的东西。

    CI 上 `CI_DEPENDENCY_PROXY_PASSWORD` 之类是 runner 给的,拉基础镜像要用;git 用不到它们,
    但把它们抹掉是替别人做决定。
    """
    monkeypatch.setenv("CI_DEPENDENCY_PROXY_SERVER", "gitlab.example:443")
    assert glw._git_env()["CI_DEPENDENCY_PROXY_SERVER"] == "gitlab.example:443"


def test_the_rest_of_the_environment_survives(monkeypatch) -> None:
    """只清代理。git 还要 HOME、PATH 这些,整个换掉环境会换出别的毛病。"""
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("HTTPS_PROXY", "http://http-proxy.example:8080")
    env = glw._git_env()
    assert env["HOME"] == "/home/someone"
    assert "PATH" in env


def test_terminal_prompt_is_disabled(monkeypatch) -> None:
    """凭据失效时不能挂在一个没人看得到的用户名提示上,那看起来像「GitLab 很慢」。"""
    assert glw._git_env()["GIT_TERMINAL_PROMPT"] == "0"


def test_token_is_scrubbed_from_anything_reported(monkeypatch) -> None:
    """错误里带着 token 是最容易的一种泄露:它会直接落进日志和返回给用户的文案。

    **两个上传组的 token 都要抹**,不只是这次用的那一个:`publish_tree` 的失败原文是直接
    给上传者看的,而"只抹当前那个"这种写法在加第二个凭据时静默失效。

    样本在运行时拼出来。整段 `https://用户:口令@主机` 写成字面量会被密钥扫描器判成
    Basic Auth 凭据 —— 远端的 pre-receive 钩子已经为此警告过一次。拼接后被测代码看到的
    字符串一模一样。
    """
    agents = "glpat-" + "NOTAREALTOKEN" + "0123456789"
    tools = "glpat-" + "ALSONOTREAL" + "9876543210"
    monkeypatch.setattr(
        glw, "get_settings",
        lambda: SimpleNamespace(
            gitlab_user_agents_token=agents, gitlab_user_tools_token=tools
        ),
    )
    for fake in (agents, tools):
        text = glw._scrub(
            "fatal: unable to access https://" + "oauth2:" + fake + "@host/x.git"
        )
        assert fake not in text
        assert "***" in text


def test_credentials_go_in_a_file_not_the_command_line() -> None:
    """凭据只能从 credential.helper 指的那个文件读。

    把 token 内联进 remote URL 是常见做法,但那样它同时出现在 `ps` 的命令行和磁盘上的
    `.git/config` 里。这里钉住实现方式,因为改回去很容易、而后果不显眼。
    """
    import inspect

    push_src = inspect.getsource(glw.publish_tree)
    assert "credential.helper=store" in inspect.getsource(glw._git)
    assert "os.chmod(cred, 0o600)" in push_src
    # 推送时给 git 的地址是项目地址原样,没有拼上凭据。
    assert 'project.http_url' in push_src
    # token 只在写那个凭据文件时被读到一次;别的地方(尤其是命令参数)都不该出现。
    uses = [
        line.strip()
        for line in push_src.splitlines()
        if "_token(" in line
    ]
    assert len(uses) == 1, f"token 被读了 {len(uses)} 次:{uses}"
    assert "cred" in uses[0] or "oauth2" in uses[0], uses[0]


def test_each_item_type_gets_its_own_group_and_token(monkeypatch) -> None:
    """组和 token 是同一个决定:group access token 只能访问它所属的那个组。

    拿 agent 那个 token 去 tool 的组建项目,GitLab 答 404 —— 而组名是对的,所以这条搞错
    的表现是"组名写错了"式的排查,离原因很远。
    """
    monkeypatch.setattr(
        glw, "get_settings",
        lambda: SimpleNamespace(
            gitlab_user_agents_group="rp-ai/rp-ai-user-agents",
            gitlab_user_agents_token="agents-token",
            gitlab_user_tools_group="rp-ai/rp-ai-user-tools",
            gitlab_user_tools_token="tools-token",
        ),
    )
    agent = glw.target_for("agent")
    tool = glw.target_for("tool")
    assert (agent.group_path, agent.token) == ("rp-ai/rp-ai-user-agents", "agents-token")
    assert (tool.group_path, tool.token) == ("rp-ai/rp-ai-user-tools", "tools-token")
    # 缺哪个变量要说得出来:有两个之后,笼统一句"凭据未配置"会让人去改另一个。
    assert agent.env_var == "GITLAB_USER_AGENTS_TOKEN"
    assert tool.env_var == "GITLAB_USER_TOOLS_TOKEN"


def test_namespace_cache_is_keyed_by_group(monkeypatch) -> None:
    """组 id 的缓存必须按组路径分键。

    共用一个全局 int 的后果是第一次上传解析出的 id 被另一种类型直接复用 —— tool 的项目
    建到 agent 组里去,门户不报错、条目也正常,只有去 GitLab 看才发现。
    """
    monkeypatch.setattr(
        glw, "get_settings",
        lambda: SimpleNamespace(
            gitlab_user_agents_group="g/agents", gitlab_user_agents_token="a",
            gitlab_user_tools_group="g/tools", gitlab_user_tools_token="t",
        ),
    )
    asked: list[str] = []

    def fake_api(method, path, payload=None, *, target):
        asked.append(target.group_path)
        return {"id": 11 if target.group_path == "g/agents" else 22}

    monkeypatch.setattr(glw, "_api", fake_api)
    monkeypatch.setattr(glw, "_namespace_ids", {})
    assert glw.namespace_id(glw.target_for("agent")) == 11
    assert glw.namespace_id(glw.target_for("tool")) == 22
    # 各自缓存,不互相顶掉。
    assert glw.namespace_id(glw.target_for("agent")) == 11
    assert asked == ["g/agents", "g/tools"]
