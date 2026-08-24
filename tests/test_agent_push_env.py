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


def test_proxy_variables_are_dropped(monkeypatch) -> None:
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://http-proxy.example:8080")
    env = glw._git_env()
    assert not [k for k in env if "proxy" in k.lower()], "代理变量必须清掉,否则推不上去"


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
    """错误里带着 token 是最容易的一种泄露:它会直接落进日志和返回给用户的文案。"""
    monkeypatch.setattr(
        glw, "get_settings",
        lambda: SimpleNamespace(gitlab_user_agents_token="glpat-SECRETVALUE123456"),
    )
    text = glw._scrub(
        "fatal: unable to access https://oauth2:glpat-SECRETVALUE123456@host/x.git"
    )
    assert "glpat-SECRETVALUE123456" not in text
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
        if "_token()" in line
    ]
    assert len(uses) == 1, f"token 被读了 {len(uses)} 次:{uses}"
    assert "cred" in uses[0] or "oauth2" in uses[0], uses[0]
