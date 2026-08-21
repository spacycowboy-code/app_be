"""内容仓储的行为约定。

这些测试盯住三件事,因为它们各自对应一个真实缺陷:
1. 读不到**必须抛**,不能悄悄换一份内容(今天 `_read_skill_md` 会回落到数据库那一列)。
2. 缓存键必须带 ref,否则同一路径的两个版本会互相污染。
3. 命中缓存**不能**再打上游,否则那 11 秒的首次生成会每次都付。
"""

from __future__ import annotations

import base64
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


def test_head_ref_returns_the_full_reference_of_the_branch(monkeypatch):
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"branches/": json.dumps(
                            {"commit": {"id": "f" * 40, "short_id": "ffffff"}}).encode()}, []))
    assert skill_content.head_ref("main") == "f" * 40


def test_head_ref_refuses_a_response_without_a_reference(monkeypatch):
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"branches/": b"{}"}, []))
    with pytest.raises(skill_content.ContentUnavailable):
        skill_content.head_ref("main")


def test_version_of_asks_for_the_last_commit_touching_that_path(monkeypatch):
    """身份必须是**这个子树自己的**。用整仓 head 当身份,任何无关提交都会把所有技能标记成有更新。"""
    calls: list[str] = []
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"commits?": json.dumps([{"id": "9" * 40}]).encode()}, calls))
    assert skill_content.version_of("official/anthropics/xlsx", at="h" * 40) == "9" * 40
    q = calls[0]
    assert "path=official%2Fanthropics%2Fxlsx" in q
    assert "ref_name=" + "h" * 40 in q
    assert "per_page=1" in q


def test_version_of_returns_none_when_the_path_has_no_history(monkeypatch):
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"commits?": b"[]"}, []))
    assert skill_content.version_of("official/nope", at="h" * 40) is None


# --- publish ---------------------------------------------------------------
#
# 前置条件的语义是实测出来的:`last_commit_id` 问的是"这个文件在这个提交之后被改过吗",
# 按文件比而不是按分支比。所以(a)一个 expect 引用能覆盖所有文件,不必逐文件查最后提交;
# (b)无关提交把分支头推走不会造成冲突;(c)冲突只可能是同一个技能被并发改了,不能重试。


def _tree_zip(prefix: str, files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, data in files.items():
            zf.writestr(f"wrapper/{prefix}/{rel}", data)
    return buf.getvalue()


@pytest.fixture
def posted(monkeypatch):
    """记下 publish 发出去的那次写,并返回一个新引用。"""
    seen: list[dict] = []

    def post(path, payload):
        seen.append({"path": path, **payload})
        return {"id": "new" + "0" * 37}

    monkeypatch.setattr(skill_content, "_post", post)
    return seen


def test_publish_creates_updates_and_deletes_to_match_the_declared_content(
    monkeypatch, posted
):
    prefix = "team/probe"
    before = {"SKILL.md": b"old", "keep.txt": b"same", "gone.txt": b"bye"}
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"archive.zip": _tree_zip(prefix, before)}, []))
    ref = skill_content.publish(
        prefix=prefix,
        files={"SKILL.md": b"new", "keep.txt": b"same", "added.txt": b"hi"},
        expect="e" * 40, branch="main", message="probe")
    assert ref.startswith("new")
    actions = posted[0]["actions"]
    by_path = {a["file_path"]: a for a in actions}
    assert by_path[f"{prefix}/SKILL.md"]["action"] == "update"
    assert by_path[f"{prefix}/added.txt"]["action"] == "create"
    assert by_path[f"{prefix}/gone.txt"]["action"] == "delete"
    assert f"{prefix}/keep.txt" not in by_path, "内容没变的文件不该出现在动作里"


def test_publish_puts_the_precondition_on_everything_that_already_exists(
    monkeypatch, posted
):
    prefix = "team/probe"
    monkeypatch.setattr(
        skill_content, "_fetch",
        _fake_fetch({"archive.zip": _tree_zip(prefix, {"SKILL.md": b"old", "x.txt": b"x"})}, []))
    skill_content.publish(prefix=prefix, files={"SKILL.md": b"new"},
                          expect="e" * 40, branch="main", message="probe")
    for a in posted[0]["actions"]:
        assert a["last_commit_id"] == "e" * 40, (
            f"{a['file_path']} 少了前置条件 —— 不带就是静默覆盖")


def test_publish_of_identical_content_does_not_commit(monkeypatch, posted):
    prefix = "team/probe"
    same = {"SKILL.md": b"same", "a.txt": b"a"}
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"archive.zip": _tree_zip(prefix, same)}, []))
    ref = skill_content.publish(prefix=prefix, files=dict(same),
                                expect="e" * 40, branch="main", message="probe")
    assert ref == "e" * 40
    assert posted == [], "内容没变却发了提交 —— 接口会产生一个空提交,版本号白涨一格"


def test_publish_of_a_new_folder_creates_everything_and_reads_nothing(
    monkeypatch, posted
):
    monkeypatch.setattr(skill_content, "_fetch",
                        lambda *a: pytest.fail("新目录不该去读旧内容"))
    skill_content.publish(prefix="team/new", files={"SKILL.md": b"x"},
                          expect=None, branch="main", message="probe")
    actions = posted[0]["actions"]
    assert [a["action"] for a in actions] == ["create"]
    assert "last_commit_id" not in actions[0]


def test_publish_can_cut_a_review_branch_in_the_same_call(monkeypatch, posted):
    monkeypatch.setattr(skill_content, "_fetch",
                        _fake_fetch({"archive.zip": _tree_zip("t/p", {})}, []))
    skill_content.publish(prefix="t/p", files={"SKILL.md": b"x"}, expect=None,
                          branch="review/abc", message="probe", start_branch="main")
    assert posted[0]["branch"] == "review/abc"
    assert posted[0]["start_branch"] == "main", (
        "复核分支必须由这次调用从远端分支切出来,否则基点是一个会漂的本地 ref")


def test_publish_sends_bytes_as_base64_so_binaries_survive(monkeypatch, posted):
    blob = bytes(range(256))
    monkeypatch.setattr(skill_content, "_fetch",
                        lambda *a: pytest.fail("新目录不该去读旧内容"))
    skill_content.publish(prefix="t/p", files={"a.bin": blob}, expect=None,
                          branch="main", message="probe")
    a = posted[0]["actions"][0]
    assert a["encoding"] == "base64"
    assert base64.b64decode(a["content"]) == blob


def test_a_missing_path_is_told_apart_from_an_upstream_failure(monkeypatch):
    """收敛器要靠这个区分"技能被删了"和"GitLab 挂了"。

    端到端验收时发现:删空一个目录之后列它,接口返回 404。两种情况合成一个异常的话,GitLab 挂一次
    就会把所有技能置成 archived。
    """
    import urllib.error

    def gone(url, *a, **kw):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    class _Opener:
        open = staticmethod(gone)

    monkeypatch.setattr(skill_content, "_opener", lambda: _Opener())
    with pytest.raises(skill_content.NotFound):
        skill_content.read("a" * 40, "team/nope/SKILL.md")
    # NotFound 是 ContentUnavailable 的子类,所以已经按"读不到"处理的调用方行为不变
    assert issubclass(skill_content.NotFound, skill_content.ContentUnavailable)


def test_a_server_error_is_not_reported_as_missing(monkeypatch):
    import urllib.error

    def broken(url, *a, **kw):
        raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)

    class _Opener:
        open = staticmethod(broken)

    monkeypatch.setattr(skill_content, "_opener", lambda: _Opener())
    with pytest.raises(skill_content.ContentUnavailable) as e:
        skill_content.read("a" * 40, "team/x/SKILL.md")
    assert not isinstance(e.value, skill_content.NotFound), "502 被当成了不存在"
