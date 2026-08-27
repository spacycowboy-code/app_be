"""tar 包:agent 能传,tool 不能,而危险成员两种格式都退回。

为什么单独一个文件而不是往 `test_agent_package.py` 里加参数化:tar 能表达的东西比 zip 多
(硬链接、设备节点、`./` 前缀、四种压缩),这些是 tar 独有的失败形状,和"包体检"那一组
断言的不是同一件事。

这里还钉住一件容易被当成优化删掉的事:tar 在送审计之前被重新打成 zip。审计引擎只解 zip,
tar 原样送过去会变成一个不透明的二进制文件 —— 一个源文件都没被扫过,而结论看起来仍然像
"通过"。那比审计失败更糟,因为失败会显示成"未能完成"。
"""
from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from app.routers import agent_upload as up
from app.services import agent_package as pkg

MAX = 50 * 1024 * 1024

README = """# my-agent

## 启动
```
python main.py --port 8756
```
"""


def mktar(
    files: dict[str, str],
    *,
    mode: str = "w",
    prefix: str = "",
    symlink: tuple[str, str] | None = None,
    hardlink: tuple[str, str] | None = None,
    fifo: str | None = None,
) -> bytes:
    """一个 tar,可选带上只有 tar 能表达的那几种成员。

    `prefix="./"` 复现 `tar -czf - .` 的产物:每一项都带 `./`,还有一个名为 `.` 的目录项。
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:  # type: ignore[call-overload]
        if prefix:
            root = tarfile.TarInfo(prefix.rstrip("/") or ".")
            root.type = tarfile.DIRTYPE
            tf.addfile(root)
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(f"{prefix}{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if symlink is not None:
            info = tarfile.TarInfo(f"{prefix}{symlink[0]}")
            info.type = tarfile.SYMTYPE
            info.linkname = symlink[1]
            tf.addfile(info)
        if hardlink is not None:
            info = tarfile.TarInfo(f"{prefix}{hardlink[0]}")
            info.type = tarfile.LNKTYPE
            info.linkname = hardlink[1]
            tf.addfile(info)
        if fifo is not None:
            info = tarfile.TarInfo(f"{prefix}{fifo}")
            info.type = tarfile.FIFOTYPE
            tf.addfile(info)
    return buf.getvalue()


def mkzip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


# --- 格式识别 ---------------------------------------------------------------


@pytest.mark.parametrize("mode", ["w", "w:gz", "w:bz2", "w:xz"])
def test_every_tar_flavour_is_recognised(mode: str, tmp_path: Path) -> None:
    """四种压缩都要认。上传者用哪一种取决于他手上的工具,不是一个我们该有意见的选择。"""
    data = mktar({"my-agent/main.py": "print(1)\n"}, mode=mode)
    assert pkg.archive_kind(data) == "tar"
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    assert [e.path for e in entries] == ["my-agent/main.py"]


def test_format_is_judged_by_content_not_extension(tmp_path: Path) -> None:
    """扩展名是上传者写的一串字符。改名成 .zip 的 tar 仍然是 tar。"""
    assert pkg.archive_kind(mktar({"a/x.py": "1"})) == "tar"
    assert pkg.archive_kind(mkzip({"a/x.py": "1"})) == "zip"
    assert pkg.archive_kind(b"neither of the two" * 50) is None


def test_a_tar_offered_where_only_zip_is_allowed_is_rejected(tmp_path: Path) -> None:
    """`allow` 是策略参数,由调用方给,而这道闸门按内容判 —— 不是按扩展名。

    上传端点现在两种类型都收 tar,所以这一条不再对应任何一个端点;留着是因为参数本身还在,
    而它是"改名成 .zip 的 tar 会不会被当成 zip 放进来"这个问题的答案。
    """
    with pytest.raises(pkg.PackageRejected) as exc:
        pkg.extract_safely(
            mktar({"a/x.py": "1"}), tmp_path / "src", max_bytes=MAX, allow=("zip",)
        )
    # 错误里要同时有"你传的是什么"和"这里收什么":只说格式不对,人会去改扩展名。
    assert "tar" in exc.value.detail and "zip" in exc.value.detail


def test_zip_still_works(tmp_path: Path) -> None:
    entries = pkg.extract_safely(
        mkzip({"a/x.py": "1"}), tmp_path / "src", max_bytes=MAX, allow=("zip",)
    )
    assert [e.path for e in entries] == ["a/x.py"]


# --- tar 独有的危险成员 -----------------------------------------------------


def test_dot_prefix_is_stripped(tmp_path: Path) -> None:
    """`tar -czf - .` 的 `./` 前缀不去掉,顶层目录会被当成 `.`。

    后果不是报错,是**静默地什么都找不到**:README、pyproject.toml、.kiro/agents/ 全都在
    `_prefixes` 之外,于是包看起来什么都没声明,而它其实什么都有。
    """
    data = mktar(
        {"my-agent/README.md": README, "my-agent/main.py": "print(1)\n"}, prefix="./"
    )
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    assert sorted(e.path for e in entries) == ["my-agent/README.md", "my-agent/main.py"]
    ins = pkg.inspect(tmp_path / "src", entries)
    assert ins.top_dir == "my-agent"
    assert ins.readme_path == "my-agent/README.md"
    assert ins.declared.launch == "python main.py --port 8756"


@pytest.mark.parametrize(
    "label, files",
    [
        ("路径穿越", {"../evil.py": "x"}),
        ("深层穿越", {"a/../../evil.py": "x"}),
        ("绝对路径", {"/etc/evil": "x"}),
        ("反斜杠", {"a\\..\\evil": "x"}),
    ],
)
def test_tar_paths_that_escape_are_rejected(
    label: str, files: dict, tmp_path: Path
) -> None:
    with pytest.raises(pkg.PackageRejected):
        pkg.extract_safely(mktar(files), tmp_path / "src", max_bytes=MAX)


def test_tar_symlink_is_rejected(tmp_path: Path) -> None:
    data = mktar({"a/x.py": "1"}, symlink=("a/link", "/etc/passwd"))
    with pytest.raises(pkg.PackageRejected) as exc:
        pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    assert "链接" in exc.value.detail


def test_tar_hardlink_is_rejected(tmp_path: Path) -> None:
    """硬链接是 tar 才有的形状,zip 里没有对应物 —— 所以它是独立的一条。"""
    data = mktar({"a/x.py": "1"}, hardlink=("a/hard", "/etc/passwd"))
    with pytest.raises(pkg.PackageRejected) as exc:
        pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    assert "链接" in exc.value.detail


def test_device_node_is_rejected_not_skipped(tmp_path: Path) -> None:
    """整包退回,不静默跳过。

    跳过的代价是审核人看到的文件清单和包里真实的内容不一样,而他是唯一的闸门。
    """
    data = mktar({"a/x.py": "1"}, fifo="a/pipe")
    with pytest.raises(pkg.PackageRejected):
        pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)


def test_nothing_is_written_outside_dest(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    with pytest.raises(pkg.PackageRejected):
        pkg.extract_safely(
            mktar({"../outside.py": "x"}), tmp_path / "src", max_bytes=MAX
        )
    assert not outside.exists()


def test_oversized_unpacked_tar_is_rejected(tmp_path: Path) -> None:
    """按解压后的大小判:gz 的压缩比可以很高,上传体积说明不了解开有多大。"""
    data = mktar({"a/big.txt": "A" * 200_000}, mode="w:gz")
    assert len(data) < 100_000
    with pytest.raises(pkg.PackageRejected):
        pkg.extract_safely(data, tmp_path / "src", max_bytes=100_000)


# --- 送审计的那份字节 -------------------------------------------------------


def test_tar_is_repacked_as_zip_for_the_audit(tmp_path: Path) -> None:
    data = mktar({"my-agent/README.md": README, "my-agent/main.py": "print(1)\n"})
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    blob, filename = up._audit_artifact(
        data, tmp_path / "src", entries, "my-agent.tar.gz"
    )
    # 引擎只解 zip(skill_audit/pipeline.py 按 is_zipfile 判)。
    assert pkg.archive_kind(blob) == "zip"
    # 文件名也要换掉:引擎会按它决定怎么落地这份附件。
    assert filename == "my-agent.zip"
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert sorted(zf.namelist()) == ["my-agent/README.md", "my-agent/main.py"]
        assert zf.read("my-agent/main.py") == b"print(1)\n"


def test_a_zip_is_sent_to_the_audit_untouched(tmp_path: Path) -> None:
    """zip 原样送。重打一遍会换掉内容哈希,而审计的缓存和留痕都按那个哈希走。"""
    data = mkzip({"a/x.py": "1"})
    entries = pkg.extract_safely(data, tmp_path / "src", max_bytes=MAX)
    blob, filename = up._audit_artifact(data, tmp_path / "src", entries, "a.zip")
    assert blob is data
    assert filename == "a.zip"


def test_repack_is_byte_identical_across_calls(tmp_path: Path) -> None:
    """体检和发布各打一次,引擎按内容哈希缓存 —— 两次不一样就要多跑一遍几十秒的审计。"""
    data = mktar({"my-agent/main.py": "print(1)\n", "my-agent/a/b.txt": "x\n"})
    first = pkg.extract_safely(data, tmp_path / "one", max_bytes=MAX)
    second = pkg.extract_safely(data, tmp_path / "two", max_bytes=MAX)
    assert pkg.repack_as_zip(tmp_path / "one", first) == pkg.repack_as_zip(
        tmp_path / "two", second
    )


# --- 类型策略 ---------------------------------------------------------------


def test_allowed_kinds_per_item_type() -> None:
    """两种类型收同一套格式。表还在,是因为"收什么"要有一处可指的地方。"""
    assert up.ALLOWED_KINDS["tool"] == ("zip", "tar")
    assert up.ALLOWED_KINDS["agent"] == ("zip", "tar")
    for itype in ("tool", "agent"):
        assert ".zip" in up.ALLOWED_SUFFIXES[itype]
        assert ".tar.gz" in up.ALLOWED_SUFFIXES[itype]


def test_item_type_defaults_to_agent_and_refuses_the_rest() -> None:
    """默认 agent:这个端点原来只有 agent,老的调用方不带这个字段。"""
    assert up._item_type(None) == "agent"
    assert up._item_type("") == "agent"
    assert up._item_type("tool") == "tool"
    # ai-platform 没有包可传,给它一个文件选择框会让人以为门户能托管它。
    with pytest.raises(Exception):
        up._item_type("ai-platform")
