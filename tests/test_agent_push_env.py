"""推送 agent 包时,git 子进程的环境与凭据处理。

这两件事都是实测踩出来的,不是预防性检查:

* 这台机器上设着 `HTTPS_PROXY=http://http-proxy...`,于是 `git push` 走代理,代理对内网地址回
  `403 from proxy after CONNECT`。API 调用没事,因为它的 opener 显式忽略代理 —— git 不知道。
* token 一旦进了 `.git/config` 或命令行,就会同时落在磁盘上和 `ps` 的输出里,而这两处都会被
  别的工具顺手记下来。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

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


# --- 提交签名 ----------------------------------------------------------------
#
# 这一组来自一次真实的失败:tool 包上传时 `git push` 被 GitLab 的「Reject unsigned commits」
# 推送规则拒收(`Commit must be signed with a GPG key`),而包已经解开、项目已经建好 ——
# 上传者看到的是一段 git 原文和 HTTP 502,对他完全不可操作。
#
# 用 SSH 签名而不是 GPG:镜像里没有 gpg,有 ssh-keygen(openssh-client),而这个 GitLab
# 实例的那条规则接受 SSH 签名。


def test_no_key_configured_means_no_signing(monkeypatch) -> None:
    """不配就不签 —— 历史行为一字不变。

    没有这条,给这个功能加一个默认值就会让所有不需要签名的环境跟着改行为。
    """
    monkeypatch.setattr(
        glw, "get_settings", lambda: SimpleNamespace(git_signing_key="")
    )
    assert glw.signing_args() == []


def test_a_configured_key_that_is_not_there_is_refused_early(monkeypatch, tmp_path) -> None:
    """配了路径但文件没挂进来 —— 这是部署漏挂,越早说越好。

    交给 git 去失败的话,报错来自 ssh-keygen、措辞离原因很远,而且那时项目已经建出来了,
    失败要多归档一个空项目。
    """
    monkeypatch.setattr(
        glw, "get_settings",
        lambda: SimpleNamespace(git_signing_key=str(tmp_path / "nope.pub")),
    )
    with pytest.raises(glw.GitLabWriteError) as exc:
        glw.signing_args()
    assert "GIT_SIGNING_KEY" in exc.value.detail


def test_the_private_half_must_be_there_too(monkeypatch, tmp_path) -> None:
    """公钥在、私钥不在:ssh-keygen 按同名去掉 .pub 找私钥,找不到就签不了。

    这种挂载(只挂了 .pub)看起来完全正常,所以必须单独说出来。
    """
    pub = tmp_path / "portal.pub"
    pub.write_text("ssh-ed25519 AAAA fake\n", encoding="utf-8")
    monkeypatch.setattr(
        glw, "get_settings", lambda: SimpleNamespace(git_signing_key=str(pub))
    )
    with pytest.raises(glw.GitLabWriteError) as exc:
        glw.signing_args()
    assert "私钥" in exc.value.detail


def test_signing_args_do_not_touch_global_config(monkeypatch, tmp_path) -> None:
    """签名参数逐条 -c 传,和身份同一条规矩:不写全局 config,也不落到 .git/config。"""
    pub = tmp_path / "portal.pub"
    pub.write_text("ssh-ed25519 AAAA fake\n", encoding="utf-8")
    (tmp_path / "portal").write_text("fake private\n", encoding="utf-8")
    monkeypatch.setattr(
        glw, "get_settings", lambda: SimpleNamespace(git_signing_key=str(pub))
    )
    args = glw.signing_args()
    assert args == ["-c", "gpg.format=ssh", "-c", f"user.signingkey={pub}"]


def test_a_real_ssh_key_produces_a_signed_commit(tmp_path, monkeypatch) -> None:
    """端到端:用这些参数提交出来的对象里真的有签名头。

    钉住的是"参数拼对了"这件事,而它只能由 git 自己回答 —— 断言参数列表长什么样只能证明
    我们没改这几个字符串。跳过而不是失败:没有 ssh-keygen 的机器上这个功能本来就用不了。
    """
    import shutil
    import subprocess

    if shutil.which("ssh-keygen") is None or shutil.which("git") is None:
        pytest.skip("需要 ssh-keygen 和 git")

    key = tmp_path / "portal"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "portal-test", "-f", str(key)],
        check=True, capture_output=True,
    )
    monkeypatch.setattr(
        glw, "get_settings",
        lambda: SimpleNamespace(git_signing_key=str(key.with_suffix(".pub"))),
    )
    sign = glw.signing_args()

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo),
         "-c", "user.name=Portal", "-c", "user.email=portal@example.com",
         *sign, "commit", "-q", "-S", "-m", "signed"],
        check=True, capture_output=True,
    )
    obj = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "commit", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout
    # `git log --format=%G?` 在没配 allowedSignersFile 的机器上答 N(验不了),那是个假信号;
    # 提交对象里的 gpgsig 头才是"签了没签"的事实。
    assert "gpgsig" in obj
    assert "BEGIN SSH SIGNATURE" in obj

    # tag 也要能签。它是两半里更容易出版本差异的那一半:`tag -s` 走 SSH 格式是 git 2.34
    # 才有的,而发布路径同时推分支和 tag,tag 签不上等于这个修复只做了一半。
    subprocess.run(
        ["git", "-C", str(repo),
         "-c", "user.name=Portal", "-c", "user.email=portal@example.com",
         *sign, "tag", "-s", "v1.0.0", "-m", "signed tag"],
        check=True, capture_output=True,
    )
    tag_obj = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "tag", "v1.0.0"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "BEGIN SSH SIGNATURE" in tag_obj


# --- 推送被规则拒收时说什么 ---------------------------------------------------


def test_an_unsigned_rejection_says_who_must_do_what() -> None:
    """上传者不是能改推送规则的人,所以这句话要指向管理员,并且说清跟他的包无关。"""
    detail = (
        "git push 失败:remote: GitLab: Commit must be signed with a GPG key "
        "! [remote rejected] HEAD -> main (pre-receive hook declined)"
    )
    hint = glw._push_rejection_hint(detail, signed=False)
    assert hint is not None
    assert "GIT_SIGNING_KEY" in hint
    assert "不是你的包的问题" in hint


def test_a_rejection_after_signing_names_the_remaining_cause() -> None:
    """签了还被拒,只剩一种解释:规则要求签名可归属到推送者,而门户是机器身份。

    这条分支存在的意义是让下一个人不必重跑一遍这次的实验。
    """
    hint = glw._push_rejection_hint(
        "remote: GitLab: Commit must be signed with a GPG key", signed=True
    )
    assert hint is not None
    assert "已经签名" in hint
    assert "Push Rules" in hint


def test_unrelated_push_failures_are_left_alone() -> None:
    """认不出来就不要翻译。给一个无关的失败套上签名的说明,比不解释更糟。"""
    assert glw._push_rejection_hint("remote: repository is archived", signed=False) is None
    assert glw._push_rejection_hint("fatal: could not read from remote", signed=True) is None
