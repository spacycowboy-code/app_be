"""内容仓储的行为约定。

这些测试盯住三件事,因为它们各自对应一个真实缺陷:
1. 读不到**必须抛**,不能悄悄换一份内容(今天 `_read_skill_md` 会回落到数据库那一列)。
2. 缓存键必须带 ref,否则同一路径的两个版本会互相污染。
3. 命中缓存**不能**再打上游,否则那 11 秒的首次生成会每次都付。
"""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from app.repositories import skill_content


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_content, "_cache_root", lambda: tmp_path)
    skill_content.STATS.hits = 0
    skill_content.STATS.misses = 0


def _fake_fetch(answers: dict[str, bytes], calls: list[str]):
    def fetch(path_and_query: str) -> bytes:
        calls.append(path_and_query)
        for needle, data in answers.items():
            if needle in path_and_query:
                return data
        raise skill_content.ContentUnavailable(f"404 {path_and_query}")
    return fetch


def test_read_returns_the_bytes_of_that_ref(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"files/": b"# hello"}, calls))
    assert skill_content.read_text("a" * 40, "x/SKILL.md") == "# hello"
    assert "ref=" + "a" * 40 in calls[0]


def test_second_read_of_the_same_ref_does_not_hit_upstream(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"files/": b"# hello"}, calls))
    ref = "b" * 40
    skill_content.read(ref, "x/SKILL.md")
    skill_content.read(ref, "x/SKILL.md")
    assert len(calls) == 1, "命中缓存还打了上游"
    assert (skill_content.STATS.hits, skill_content.STATS.misses) == (1, 1)


def test_a_different_ref_is_a_different_cache_entry(monkeypatch):
    answers = {"ref=" + "c" * 40: b"old", "ref=" + "d" * 40: b"new"}
    calls: list[str] = []
    monkeypatch.setattr(skill_content, "_fetch", _fake_fetch(answers, calls))
    assert skill_content.read("c" * 40, "x/SKILL.md") == b"old"
    assert skill_content.read("d" * 40, "x/SKILL.md") == b"new"


def test_unreachable_raises_instead_of_returning_empty(monkeypatch):
    def boom(_: str) -> bytes:
        raise skill_content.ContentUnavailable("URLError")
    monkeypatch.setattr(skill_content, "_fetch", boom)
    with pytest.raises(skill_content.ContentUnavailable):
        skill_content.read("e" * 40, "x/SKILL.md")


def test_no_ref_is_refused_rather_than_guessed(monkeypatch):
    monkeypatch.setattr(skill_content, "_fetch", lambda _: b"whatever")
    with pytest.raises(skill_content.ContentUnavailable):
        skill_content.read("", "x/SKILL.md")


def test_list_files_walks_all_pages_and_keeps_only_blobs(monkeypatch):
    page1 = [{"type": "blob", "path": f"x/f{i}"} for i in range(100)]
    page2 = [{"type": "tree", "path": "x/sub"}, {"type": "blob", "path": "x/last"}]
    seen: list[str] = []

    def fetch(path_and_query: str) -> bytes:
        seen.append(path_and_query)
        # 注意用 "&page=" 而不是 "page=" —— 查询里有 per_page=100,
        # 拿 "page=1" 去匹配会命中 per_pa"ge=1"00,假上游永远返回第一页。
        return json.dumps(page1 if "&page=1" in path_and_query else page2).encode()

    monkeypatch.setattr(skill_content, "_fetch", fetch)
    files = skill_content.list_files("f" * 40, "x")
    assert len(files) == 101 and "x/last" in files
    assert "x/sub" not in files, "目录被当成文件了"
    assert len(seen) == 2, "第二页没取"


def test_archive_comes_back_as_a_readable_tar(monkeypatch):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"# md"
        info = tarfile.TarInfo("skill-abc/SKILL.md")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"archive.tar.gz": buf.getvalue()}, []))
    raw = skill_content.archive("0" * 40, "x")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        assert [m.name for m in tf if m.isfile()] == ["skill-abc/SKILL.md"]

def test_a_tree_that_never_ends_is_refused_instead_of_eating_memory(monkeypatch):
    """上游一直返回满页时必须停下来。第一次写这个测试时它把容器 OOM 杀掉了。"""
    full = [{"type": "blob", "path": f"x/f{i}"} for i in range(100)]
    monkeypatch.setattr(skill_content, "_fetch",
                        lambda _: json.dumps(full).encode())
    monkeypatch.setattr(skill_content, "_MAX_TREE_PAGES", 3)
    with pytest.raises(skill_content.ContentUnavailable):
        skill_content.list_files("9" * 40, "x")
