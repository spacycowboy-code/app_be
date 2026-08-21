"""内容仓储的行为约定。

这些测试盯住三件事,因为它们各自对应一个真实缺陷:
1. 读不到**必须抛**,不能悄悄换一份内容(今天 `_read_skill_md` 会回落到数据库那一列)。
2. 缓存键必须带 ref,否则同一路径的两个版本会互相污染。
3. 命中缓存**不能**再打上游,否则那 11 秒的首次生成会每次都付。
"""

from __future__ import annotations

import io
import json
import zipfile

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


def test_read_tree_strips_both_the_gitlab_wrapper_and_the_skill_path(monkeypatch):
    """归档里有两层前缀,调用方要的是"相对技能目录"的路径。"""
    prefix = "team/probe_EXXX_Probe"
    wrapper = f"rp-ai-skill-{'1' * 40}-{'1' * 40}-team-probe"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{wrapper}/{prefix}/SKILL.md", b"# md")
        zf.writestr(f"{wrapper}/{prefix}/scripts/run.py", b"print(1)")
        zf.writestr(f"{wrapper}/{prefix}/artifacts/orig.zip", b"PK\x03\x04zip")
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"archive.zip": buf.getvalue()}, []))
    tree = skill_content.read_tree("1" * 40, prefix)
    assert sorted(tree) == ["SKILL.md", "artifacts/orig.zip", "scripts/run.py"]
    assert tree["SKILL.md"] == b"# md"


def test_read_tree_refuses_a_corrupt_archive_instead_of_returning_nothing(monkeypatch):
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"archive.zip": b"not a zip at all"}, []))
    with pytest.raises(skill_content.ContentUnavailable):
        skill_content.read_tree("2" * 40, "team/probe")


def test_read_tree_is_one_request_even_for_many_files(monkeypatch):
    """整棵子树一次拿回。逐文件读的话官方技能 63 个文件要 30 秒。"""
    prefix = "official/anthropics/docx"
    wrapper = "rp-ai-skill-abc-abc-official-anthropics-docx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(63):
            zf.writestr(f"{wrapper}/{prefix}/f{i}.txt", b"x")
    calls: list[str] = []
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"archive.zip": buf.getvalue()}, calls))
    tree = skill_content.read_tree("3" * 40, prefix)
    assert len(tree) == 63
    assert len(calls) == 1
