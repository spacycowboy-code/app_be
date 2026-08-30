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

from sqlalchemy import select

from app.database import SessionLocal
from app.models.user import RpAiPortal
from app.routers.agent_upload import _ensure_user
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


# --- 撞名 --------------------------------------------------------------------
#
# 这一组来自第三次上传失败。前两次因为推送规则倒在 git push,失败清理把刚建的项目归档了 ——
# 而**归档不释放名字**。于是同一个包第三次上传连项目都建不出来:
#
#   GitLab 返回 HTTP 400:{"message":{"project_namespace.name":["has already been taken"],
#                                    "name":["has already been taken"]}}
#
# 两个独立的缺陷凑在一起才有这个现象:重试只给 path 加序号(名字永远撞),而第 5 轮又把
# GitLab 的 JSON 原文抛出去(循环末尾那句人话永远走不到)。


def _target(monkeypatch) -> "glw.UploadTarget":
    monkeypatch.setattr(
        glw, "get_settings",
        lambda: SimpleNamespace(
            gitlab_user_agents_group="g/agents", gitlab_user_agents_token="a",
            gitlab_user_tools_group="g/tools", gitlab_user_tools_token="t",
        ),
    )
    monkeypatch.setattr(glw, "_namespace_ids", {"g/tools": 22, "g/agents": 11})
    return glw.target_for("tool")


def test_a_taken_name_is_retried_with_a_suffixed_name_too(monkeypatch) -> None:
    """序号要同时加在路径和名字上。

    只改 path 的话,一个被占用的名字会让 5 次重试全部撞在同一处 —— 这正是第三次上传失败的
    原因,而报错和真正的起因(前两次留下的归档项目)看不出任何关系。
    """
    target = _target(monkeypatch)
    seen: list[tuple[str, str]] = []

    def fake_api(method, path, payload=None, *, target):
        seen.append((payload["name"], payload["path"]))
        if payload["name"] == "Generate EEF":      # 名字被归档项目占着
            raise glw.GitLabWriteError(
                'GitLab 返回 HTTP 400:{"message":{"name":["has already been taken"]}}'
            )
        return {
            "id": 7, "path_with_namespace": f"g/tools/{payload['path']}",
            "web_url": "http://x", "http_url_to_repo": "http://x.git",
        }

    monkeypatch.setattr(glw, "_api", fake_api)
    ref = glw.create_project(slug="generate-eef", name="Generate EEF", target=target)
    assert ref.id == 7
    # 第一轮用原名原路径,第二轮两个都带序号。
    assert seen == [("Generate EEF", "generate-eef"), ("Generate EEF (2)", "generate-eef-2")]


def test_running_out_of_names_says_what_is_holding_them(monkeypatch) -> None:
    """5 轮都被占时给的是人话,而且指向"归档项目仍占名字"这个真实原因。

    原来第 5 轮直接把 GitLab 的 JSON 原文抛给上传者,他从那段 JSON 里既看不出该找谁,也看不出
    这跟他的包无关。
    """
    target = _target(monkeypatch)

    def always_taken(method, path, payload=None, *, target):
        raise glw.GitLabWriteError(
            'GitLab 返回 HTTP 400:{"message":{"name":["has already been taken"]}}'
        )

    monkeypatch.setattr(glw, "_api", always_taken)
    with pytest.raises(glw.GitLabWriteError) as exc:
        glw.create_project(slug="generate-eef", name="Generate EEF", target=target)
    detail = exc.value.detail
    assert "has already been taken" not in detail       # 不再是 GitLab 的原文
    assert "归档" in detail and "g/tools" in detail      # 说出原因和该去哪个组找


def test_a_real_error_is_not_swallowed_by_the_retry(monkeypatch) -> None:
    """只有"名字被占"才重试。别的失败必须立刻抛出去,否则一个凭据问题会被重试 5 次。"""
    target = _target(monkeypatch)
    calls = {"n": 0}

    def refuse(method, path, payload=None, *, target):
        calls["n"] += 1
        raise glw.CredentialExpired("凭据已失效")

    monkeypatch.setattr(glw, "_api", refuse)
    with pytest.raises(glw.CredentialExpired):
        glw.create_project(slug="x", name="X", target=target)
    assert calls["n"] == 1


def test_rename_frees_the_name(monkeypatch) -> None:
    """改名走 PUT /projects/:id,path 和 name 一起改 —— 只改一个,另一个仍然占着。"""
    target = _target(monkeypatch)
    sent: dict = {}

    def fake_api(method, path, payload=None, *, target):
        sent.update({"method": method, "path": path, "payload": payload})
        return {}

    monkeypatch.setattr(glw, "_api", fake_api)
    glw.rename_project(7, path="x-failed-20260830", name="X (failed)", target=target)
    assert sent["method"] == "PUT"
    assert sent["path"] == "projects/7"
    assert sent["payload"] == {"path": "x-failed-20260830", "name": "X (failed)"}


# --- 上传者那一行 -------------------------------------------------------------
#
# 这一组来自第一次真正推送成功的上传:GitLab 那一整段都过了(项目建出来、签名的提交和 tag
# 都推上去了),然后倒在写库,门户答 400「Database constraint violation」而日志里一个字都没有。
#
# 真正的原因是 _ensure_user 用 db.get() 按主键查 eid —— 而 rp_ai_portal 的主键是自增的 id,
# eid 只是一个带唯一索引的普通列。db.get 永远答 None,于是每次上传都插一行重复的 eid:
#
#   (1062, "Duplicate entry 'EZHGMIO' for key 'rp_ai_portal.ix_rp_ai_portal_eid'")
#
# 这段代码在生产从未跑到过(推送更早就失败了),所以这个洞从上线活到现在。


def test_ensure_user_finds_an_existing_person_by_eid() -> None:
    """已经在库里的人不能被再插一次。

    这条钉的是"按什么查",不是"插不插" —— db.get(RpAiPortal, eid) 按主键(自增 id)查一个
    EID 字符串,永远答 None,于是永远走进插入分支。
    """
    with SessionLocal() as db:
        db.add(RpAiPortal(eid="EZHGMIO", display_name="Miao Zhang M"))
        db.commit()

        _ensure_user(db, "EZHGMIO")      # 不许抛,也不许再插一行
        db.commit()

        rows = db.scalars(select(RpAiPortal).where(RpAiPortal.eid == "EZHGMIO")).all()
        assert len(rows) == 1
        # 原来那一行要保持原样:插新行会把 display_name 冲成空。
        assert rows[0].display_name == "Miao Zhang M"


def test_ensure_user_still_creates_someone_who_is_missing() -> None:
    """第一次上传的人不在表里 —— 版本行的 uploader_eid 是外键,这一行必须先有。"""
    with SessionLocal() as db:
        assert db.scalar(select(RpAiPortal).where(RpAiPortal.eid == "ENEWGUY")) is None
        _ensure_user(db, "ENEWGUY")
        db.commit()
        assert db.scalar(select(RpAiPortal).where(RpAiPortal.eid == "ENEWGUY")) is not None


def test_eid_is_not_the_primary_key() -> None:
    """把这个前提本身钉住。

    这张表哪天真把 eid 变成主键,上面两条会照样通过而 db.get 也重新变得正确 —— 但在那之前,
    任何"拿 EID 当主键查"的写法都是错的,而它不报错、只是静默地永远查不到人(审核页的上传者
    姓名就是这么丢的)。
    """
    assert [c.name for c in RpAiPortal.__table__.primary_key] == ["id"]
    with SessionLocal() as db:
        db.add(RpAiPortal(eid="EZHGMIO"))
        db.commit()
        # 人就在库里,db.get 照样答 None —— 这就是那个洞。
        assert db.get(RpAiPortal, "EZHGMIO") is None


def test_failed_push_renames_before_it_archives() -> None:
    """失败清理的顺序必须是先改名、再归档。

    顺序反了就白改:归档后的项目是只读的,改名会被 GitLab 拒,于是这个包的名字仍然被永久
    占掉 —— 而这两句挨在一起、看起来可以随便换位置,所以在这里钉住。

    端到端跑一遍 publish_package 需要造 multipart 上传、打桩审计引擎和整套 GitLab API,
    这个仓里还没有那套脚手架;按同文件 test_credentials_go_in_a_file 的先例,用源码顺序
    断言换取覆盖。
    """
    import inspect

    from app.routers import agent_upload as au

    src = inspect.getsource(au.publish_package)
    rename_at = src.find("glw.rename_project(")
    archive_at = src.find("glw.archive_project(")
    assert rename_at != -1, "推送失败后没有给项目改名,名字会被永久占掉"
    assert archive_at != -1, "推送失败后没有归档,组里会留下一个看不出是半成品的空项目"
    assert rename_at < archive_at, "改名必须在归档之前:归档后的项目是只读的,改不了名"
    # 改名失败不能吞掉真正的失败原因 —— 上传者要看到的是推送为什么被拒。
    assert src.count("except glw.GitLabWriteError:") >= 2
