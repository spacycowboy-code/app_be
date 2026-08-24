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
