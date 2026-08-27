"""Agent 包体检:什么必须被退回,什么只是提示。

这里钉住的是**事实类**判断,不涉及审计结论(那个只出结果不拦截)。两类必须退回:解压会写到
目标目录外面的包,和带明文凭据的包(GitLab 的 pre-receive 钩子必定拒收,提前说只是为了让
失败发生在第一秒而不是第两分钟)。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.services import agent_package as pkg

MAX = 50 * 1024 * 1024


def mkzip(files: dict[str, str], *, symlink: tuple[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink[0])
            info.external_attr = (0xA000 | 0o777) << 16   # S_IFLNK
            z.writestr(info, symlink[1])
    return buf.getvalue()


README = """# my-agent

一个测试用的 agent。

## 安装
```
pip install -r requirements.txt
```

## 启动
```
python main.py --port 8756
```
"""


def test_normal_package_is_inspected(tmp_path: Path) -> None:
    data = mkzip({
        "my-agent/README.md": README,
        "my-agent/main.py": "print('hi')\n",
        "my-agent/requirements.txt": "flask\n",
    })
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    ins = pkg.inspect(tmp_path / "src", entries)

    assert ins.top_dir == "my-agent"
    assert ins.readme_path == "my-agent/README.md"
    # 装依赖那一行不能被当成启动命令:选错了,用户会在装依赖那步以为已经启动。
    assert ins.declared.launch == "python main.py --port 8756"
    assert not any("pip install" in c for c in ins.launch_candidates)
    # 来源必须写清楚:一个自己填好的值,看不出哪来的就没法核对。
    assert "README" in ins.declared.sources["launch"]
    assert pkg.launch_target_exists(ins.declared.launch, entries) is True


def test_launch_target_missing_is_reported_not_rejected(tmp_path: Path) -> None:
    """启动命令指向包里没有的文件 —— 只提示,不拦。

    可能是 pip 装出来的 entry point,所以判断不了对错;但这是最容易错的一项,得让人看见。
    """
    data = mkzip({"a/README.md": "## 启动\n```\npython server.py\n```\n"})
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    ins = pkg.inspect(tmp_path / "src", entries)
    assert ins.declared.launch == "python server.py"
    assert pkg.launch_target_exists("python server.py", entries) is False


@pytest.mark.parametrize(
    "label, files",
    [
        ("路径穿越", {"../evil.py": "x"}),
        ("深层穿越", {"a/../../evil.py": "x"}),
        ("绝对路径", {"/etc/evil": "x"}),
        ("Windows 盘符", {"C:/evil": "x"}),
        ("反斜杠", {"a\\..\\evil": "x"}),
    ],
)
def test_paths_that_escape_are_rejected(label: str, files: dict, tmp_path: Path) -> None:
    """zip slip:这不是对上传者的判断,是我们自己会写到目录外面。"""
    with pytest.raises(pkg.PackageRejected):
        pkg.extract_safely(mkzip(files), tmp_path / "src", max_bytes=MAX)


def test_symlink_is_rejected(tmp_path: Path) -> None:
    data = mkzip({"a/x.py": "1"}, symlink=("a/link", "/etc/passwd"))
    with pytest.raises(pkg.PackageRejected) as exc:
        pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    assert "符号链接" in exc.value.detail


def test_nothing_is_written_outside_dest(tmp_path: Path) -> None:
    """退回之后,目标目录之外一个文件都不该出现。"""
    outside = tmp_path / "outside.py"
    dest = tmp_path / "src"
    with pytest.raises(pkg.PackageRejected):
        pkg.extract_safely(mkzip({"../outside.py": "x"}), dest, max_bytes=MAX)
    assert not outside.exists()


def test_not_a_zip_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(pkg.PackageRejected):
        pkg.extract_safely(b"not a zip at all", tmp_path / "src", max_bytes=MAX)


def test_oversized_unpacked_size_is_rejected(tmp_path: Path) -> None:
    """压缩比很高的包解开可能远大于上传体积,按解压后的大小判。"""
    data = mkzip({"a/big.txt": "A" * 200_000})
    with pytest.raises(pkg.PackageRejected):
        pkg.extract_safely(data, tmp_path / "src", max_bytes=100_000)


# 这两个样本在运行时拼出来,而不是当字面量写在文件里。理由很实在:GitLab 的 pre-receive
# 密钥扫描会拦下含这类字面量的提交 —— 也就是这个测试要验证的那件事,会先把它自己挡在门外。
# 拼接后扫描器看到的字符串完全一样,测的东西没有变。
FAKE_PAT = "glpat-" + "abcdefghijklmnopqrstuvwx"
FAKE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"


def test_plaintext_credentials_are_found(tmp_path: Path) -> None:
    data = mkzip({
        "a/settings.py": f'GITLAB = "{FAKE_PAT}"\n',
        "a/key.pem": f"{FAKE_KEY_HEADER}\nMIIE\n",
        "a/README.md": "# ok\n",
    })
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    hits = pkg.scan_secrets(tmp_path / "src", entries)
    rules = {h["rule"] for h in hits}
    assert rules == {"gitlab_pat", "private_key"}
    # 位置要具体到行:让上传者能直接去改,而不是重新读一遍整个包。
    assert all(h["line"] >= 1 and h["file"] for h in hits)


def test_placeholder_keys_are_not_flagged(tmp_path: Path) -> None:
    """文档里的占位符不算凭据 —— 这一项会拦上传,误报的代价是有人传不上本来没问题的包。"""
    data = mkzip({"a/README.md": "把 " + "sk-" + "YOUR_KEY_HERE 换成你自己的\n"})
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    assert pkg.scan_secrets(tmp_path / "src", entries) == []


def test_sidecar_beats_readme(tmp_path: Path) -> None:
    """包里带了 portal-agent.yaml 就用它,不再猜 README。"""
    data = mkzip({
        "a/README.md": README,
        "a/portal-agent.yaml": (
            'launch: "python -m myagent --headless"\n'
            "runtime: long-running\n"
            "keep:\n  - .env\n  - data/*.db\n"
        ),
    })
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    ins = pkg.inspect(tmp_path / "src", entries)
    assert ins.declared.launch == "python -m myagent --headless"
    assert ins.declared.runtime == "long-running"
    assert ins.declared.keep == [".env", "data/*.db"]
    assert ins.declared.sources["launch"] == "portal-agent.yaml"


def test_broken_sidecar_is_reported_not_silently_ignored(tmp_path: Path) -> None:
    data = mkzip({"a/portal-agent.yaml": "launch: [unclosed\n"})
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    declared = pkg.read_sidecar(tmp_path / "src", entries)
    assert declared.launch is None
    assert "解析失败" in declared.sources["sidecar"]


def test_unknown_runtime_is_dropped(tmp_path: Path) -> None:
    """runtime 决定提示词里要不要写"会弹出浏览器窗口",所以不认识的值一律作废。"""
    data = mkzip({"a/portal-agent.yaml": 'launch: "x"\nruntime: whenever\n'})
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    assert pkg.read_sidecar(tmp_path / "src", entries).runtime is None


def test_rendered_sidecar_round_trips(tmp_path: Path) -> None:
    """生成的文件必须能被自己读回来 —— 命令里常有引号和反斜杠。"""
    declared = pkg.Declared(
        launch='python main.py --path "C:\\Users\\me" --note it\'s',
        runtime="long-running",
        keep=[".env"],
    )
    text = pkg.render_sidecar(declared, name="my agent", version="1.2.0")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / pkg.SIDECAR_NAME).write_text(text, encoding="utf-8")
    back = pkg.read_sidecar(tmp_path / "src", [pkg.FileEntry(pkg.SIDECAR_NAME, len(text))])
    assert back.launch == declared.launch
    assert back.runtime == "long-running"
    assert back.keep == [".env"]


def test_git_dir_and_env_files_are_surfaced(tmp_path: Path) -> None:
    """两件上传者该知道的事:包里的 .git 会被丢掉,.env 通常是不该进包的。"""
    data = mkzip({
        "a/.git/config": "[remote]\n",
        "a/.env": "TOKEN=placeholder\n",
        "a/main.py": "1\n",
    })
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    ins = pkg.inspect(tmp_path / "src", entries)
    assert ins.has_git_dir is True
    assert ins.env_files == ["a/.env"]
    assert "a/main.py" in ins.executables


# --- 启动命令的结构化推导 ---------------------------------------------------
#
# 这一组钉住的是**来源的优先级**,而不是"能不能推出来"。理由:每个来源单独看都能给出一条
# 像样的命令,错的只是选了哪一条 —— 而选错的表现是用户粘贴后启动了另一个程序,那行命令
# 看起来完全正常。


def inspect_zip(files: dict[str, str], tmp_path: Path) -> pkg.Inspection:
    data = mkzip(files)
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    return pkg.inspect(tmp_path / "src", entries)


KIRO_AGENT = '{"name": "timesheet", "description": "报工时助手", "tools": ["read"]}'


def test_kiro_agent_config_beats_readme_and_pyproject(tmp_path: Path) -> None:
    """report-manhour 的真实形状:三个来源同时存在,只有一个是市场里的人要的那条。

    `pyproject.toml` 的 `timesheet` 是给这个工具的开发者用的底层 CLI;README 的
    `python -m timesheet ...` 同理。用户要的是那个交互式会话,只有 `.kiro/agents/` 知道。
    """
    ins = inspect_zip({
        "report-manhour/README.md": (
            "# 自动报工时\n\n## 启动\n```\npython -m timesheet --input x.xlsx\n```\n"
        ),
        "report-manhour/pyproject.toml": (
            '[project]\nname = "timesheet"\n\n'
            '[project.scripts]\ntimesheet = "timesheet.cli:main"\n'
        ),
        "report-manhour/.kiro/agents/timesheet.json": KIRO_AGENT,
    }, tmp_path)

    assert ins.declared.launch == "kiro-cli chat --agent timesheet"
    # 确定推导出来的,确认屏可以当已核对的值显示 —— 所以来源里不该出现"待核对"。
    assert ins.launch_guesses[0].certain is True
    assert "待核对" not in ins.declared.sources["launch"]
    assert ".kiro/agents/timesheet.json" in ins.declared.sources["launch"]
    # 别的来源仍然作为候选留着,只是排在后面。
    assert "timesheet" in ins.launch_candidates


def test_readme_placeholder_line_is_not_prefilled(tmp_path: Path) -> None:
    """回归:`--input "..."` 这种模板行曾被排到第一位并预填进确认屏。

    它不装依赖、也在"启动"标题下面,所以前面两道过滤都放它过去了。粘贴出去的结果是找不到
    名叫 `...` 的文件,而那行命令看起来完全正常。
    """
    ins = inspect_zip({
        "a/README.md": (
            "## 启动\n```\n"
            'python -m timesheet --input "..." --force-rewrite\n'
            "python -m timesheet --survey\n"
            "```\n"
        ),
    }, tmp_path)

    assert '"..."' not in (ins.declared.launch or "")
    assert ins.declared.launch == "python -m timesheet --survey"
    assert not any("..." in c for c in ins.launch_candidates)


def test_multiple_kiro_agents_are_not_prefilled_as_certain(tmp_path: Path) -> None:
    """替人选一个,他点下一步时不会发现选错了;全列出来只是多点一下。"""
    ins = inspect_zip({
        "a/.kiro/agents/one.json": '{"name": "one"}',
        "a/.kiro/agents/two.json": '{"name": "two"}',
    }, tmp_path)

    assert len(ins.launch_guesses) == 2
    assert all(g.certain is False for g in ins.launch_guesses)
    assert set(ins.launch_candidates) == {
        "kiro-cli chat --agent one", "kiro-cli chat --agent two",
    }
    assert "待核对" in ins.declared.sources["launch"]


def test_agent_name_not_matching_filename_is_flagged(tmp_path: Path) -> None:
    """kiro-cli 认文件名还是认 json 里的 name,我们没有权威依据 —— 所以不赌。

    取文件名并要求两者一致,不一致时降级成待核对并把不一致本身说出来。赌错的表现是
    "agent not found",看起来像根本没装上。
    """
    ins = inspect_zip({
        "a/.kiro/agents/timesheet.json": '{"name": "report-manhour"}',
    }, tmp_path)

    assert ins.launch_guesses[0].certain is False
    source = ins.declared.sources["launch"]
    assert "timesheet" in source and "report-manhour" in source


def test_broken_agent_json_is_ignored(tmp_path: Path) -> None:
    """坏掉的 json 可能根本不是 agent 配置。按文件名硬推,错的时候看不出来。"""
    ins = inspect_zip({
        "a/.kiro/agents/x.json": "{not json at all",
        "a/README.md": "## 启动\n```\npython main.py\n```\n",
    }, tmp_path)

    assert ins.declared.launch == "python main.py"


def test_sidecar_still_beats_structured_derivation(tmp_path: Path) -> None:
    """上传者明确写过的东西,不该被我们推导出来的值盖掉。"""
    ins = inspect_zip({
        "a/.kiro/agents/timesheet.json": KIRO_AGENT,
        "a/portal-agent.yaml": 'launch: "kiro-cli chat --agent timesheet --resume"\n',
    }, tmp_path)

    assert ins.declared.launch == "kiro-cli chat --agent timesheet --resume"
    assert ins.declared.sources["launch"] == "portal-agent.yaml"


def test_pyproject_scripts_when_there_is_no_agent_config(tmp_path: Path) -> None:
    ins = inspect_zip({
        "a/pyproject.toml": '[project.scripts]\nmytool = "mytool.cli:main"\n',
    }, tmp_path)
    assert ins.declared.launch == "mytool"
    assert ins.launch_guesses[0].certain is True


def test_package_json_start_script(tmp_path: Path) -> None:
    ins = inspect_zip({
        "a/package.json": '{"name": "x", "scripts": {"start": "node server.js"}}',
    }, tmp_path)
    assert ins.declared.launch == "npm start"


# --- runtime 与 keep 的推导 -------------------------------------------------


def test_browser_automation_implies_long_running(tmp_path: Path) -> None:
    ins = inspect_zip({
        "a/requirements.txt": "playwright==1.49.1\nopenpyxl==3.1.5\n",
    }, tmp_path)
    assert ins.declared.runtime == "long-running"
    assert "待核对" in ins.declared.sources["runtime"]


def test_runtime_is_left_empty_without_a_strong_signal(tmp_path: Path) -> None:
    """猜错的表现是对一个两秒就退出的脚本说"别关掉那个窗口"。留空让上传者选。"""
    ins = inspect_zip({"a/requirements.txt": "requests\n"}, tmp_path)
    assert ins.declared.runtime is None


REAL_GITIGNORE = """\
# 登录态：含 cookie 与浏览器 profile，绝不入库
.browser-profile/

# 运行产物：审计日志、计划报告、失败截图
.timesheet-runs/

# 输入物料（可能含真实项目号与工号）
*.xlsx
*.png
!tests/fixtures/*.png

# 代理生成的一次性启动器
.ts-input.json

# Python
__pycache__/
.venv/
*.egg-info/
build/
dist/
.pytest_cache/
.mypy_cache/
"""


def test_keep_comes_from_gitignore_without_build_artifacts(tmp_path: Path) -> None:
    """被 .gitignore 忽略的正好是"包里没有、远端也没有、只在这台机器上"的那一批。

    排掉两类:构建产物(不搬不丢东西)和裸扩展名通配(匹配太宽,而提示词第 3 步的
    "凡是只存在于 .bak 的都要搬"已经覆盖了它们)。
    """
    ins = inspect_zip({
        "a/.gitignore": REAL_GITIGNORE,
        "a/main.py": "1\n",
    }, tmp_path)

    keep = ins.declared.keep
    # 丢了就恢复不回来的两项必须在。
    assert ".browser-profile" in keep
    assert ".timesheet-runs" in keep
    assert ".ts-input.json" in keep
    # 构建产物与工具缓存一个都不该在。
    for derived in ("__pycache__", ".venv", "build", "dist", ".pytest_cache", ".mypy_cache"):
        assert derived not in keep
    # 裸扩展名通配不进清单。
    assert not any(k.startswith("*.") for k in keep)
    assert "请删掉不需要搬的" in ins.declared.sources["keep"]


def test_sidecar_keep_is_not_overwritten_by_derivation(tmp_path: Path) -> None:
    ins = inspect_zip({
        "a/.gitignore": REAL_GITIGNORE,
        "a/portal-agent.yaml": 'launch: "x"\nkeep:\n  - only-this\n',
    }, tmp_path)
    assert ins.declared.keep == ["only-this"]


def test_no_gitignore_means_no_keep_candidates(tmp_path: Path) -> None:
    ins = inspect_zip({"a/main.py": "1\n"}, tmp_path)
    assert ins.declared.keep == []


# --- 可用性检查 -------------------------------------------------------------


def test_kiro_launch_resolves_to_the_agent_config(tmp_path: Path) -> None:
    """`kiro-cli chat --agent X` 里一个词都不是包内文件,但它确实指向 `.kiro/agents/X.json`。

    按通用规则它一定判 False,于是最常见也最有把握的那种包每次都会亮一条"包里找不到这个
    文件"的警告 —— 而一处明显不对的提醒会让人连带不信别的提醒。
    """
    data = mkzip({"a/.kiro/agents/timesheet.json": KIRO_AGENT})
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    assert pkg.launch_target_exists("kiro-cli chat --agent timesheet", entries) is True


def test_wrong_agent_name_is_still_caught(tmp_path: Path) -> None:
    """上面那条放宽不能把检查放空:agent 名写错了照样要判 False。"""
    data = mkzip({"a/.kiro/agents/timesheet.json": KIRO_AGENT})
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    assert pkg.launch_target_exists("kiro-cli chat --agent timesheeet", entries) is False


# --- 推导不能被包里的内容带跑 -----------------------------------------------


@pytest.mark.parametrize("name", ["x; whoami", "a b", "$(id)", "x&&y", "x|y", "x`id`"])
def test_agent_name_with_shell_metacharacters_yields_nothing(name: str, tmp_path: Path) -> None:
    """推出来的命令会被标成"已确定、无需核对",然后原样交给使用者的 agent 去执行。

    上传者本来就能在表单里手填任何启动命令,所以这不是新增的权限;新增的是"这条不用核对"
    这个信号,而那正是不能给一条 `;` 分隔的命令的东西。名字不合规就整条不出。
    """
    ins = inspect_zip({f"a/.kiro/agents/{name}.json": "{}"}, tmp_path)
    assert ins.launch_guesses == []
    assert ins.declared.launch is None


def test_agent_name_charset_matches_the_availability_check(tmp_path: Path) -> None:
    """两边字符集必须一致,否则确认屏一边说"已确定"、一边说"包里找不到"。"""
    ins = inspect_zip({"a/.kiro/agents/ts-report_2.json": "{}"}, tmp_path)
    assert ins.declared.launch == "kiro-cli chat --agent ts-report_2"
    assert ins.launch_guesses[0].certain is True
    entries = pkg.extract_safely(
        mkzip({"a/.kiro/agents/ts-report_2.json": "{}"}), tmp_path / "src2", max_bytes=MAX
    )
    assert pkg.launch_target_exists(ins.declared.launch, entries) is True


@pytest.mark.parametrize("body", [
    'project = "not a table"',          # 合法 TOML,但 project 不是表
    "project = 1",
    "[project]\nscripts = 'nope'",      # scripts 不是表
])
def test_malformed_pyproject_does_not_crash_the_inspection(body: str, tmp_path: Path) -> None:
    """这是上传者的文件。抛 AttributeError 的结果是体检端点 500,而他看到的是
    "服务器错误",不是"你的 pyproject 有问题"。"""
    ins = inspect_zip({"a/pyproject.toml": body + "\n"}, tmp_path)
    assert ins.declared.launch is None


def test_dev_only_browser_dependency_is_not_long_running(tmp_path: Path) -> None:
    """一个用 playwright 做端到端测试的普通应用,跑起来根本不开浏览器。

    判成常驻,提示词就会对一个两秒退出的脚本说"别关掉那个窗口"。
    """
    ins = inspect_zip({
        "a/pyproject.toml": (
            '[project]\ndependencies = ["flask"]\n\n'
            '[project.optional-dependencies]\ndev = ["pytest-playwright"]\n'
        ),
    }, tmp_path)
    assert ins.declared.runtime is None


def test_runtime_browser_dependency_still_detected_in_pyproject(tmp_path: Path) -> None:
    ins = inspect_zip({
        "a/pyproject.toml": '[project]\ndependencies = ["playwright==1.49.1"]\n',
    }, tmp_path)
    assert ins.declared.runtime == "long-running"


def test_node_dev_dependency_is_not_long_running(tmp_path: Path) -> None:
    ins = inspect_zip({
        "a/package.json": (
            '{"name": "x", "dependencies": {"express": "^4"},'
            ' "devDependencies": {"playwright": "^1.49"}}'
        ),
    }, tmp_path)
    assert ins.declared.runtime is None
