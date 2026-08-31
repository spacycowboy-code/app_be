"""Gitiles 客户端拼出来的是什么 URL。

只有一条是真正重要的:取对象必须用 ``+show/`` 而不是 ``+/``。实测
``+/<ref>/skill/rtm-3211/manifest.yaml?format=TEXT`` 是 200,同一条路径换成 ``SKILL.md``
是 **400** —— Gitiles 对 ``.md`` 走文档渲染那条路,和 ``format=TEXT`` 冲突。技能的正文恰好
全是 ``.md``,所以这个坑只在真的去读正文时才撞上,而那已经是同步之后的事了。
"""
import base64

import pytest

from app.config import get_settings
from app.repositories import gerrit_content


@pytest.fixture
def captured(monkeypatch):
    """记下每次请求的 path,并回一个能解析的响应。"""
    seen: list[str] = []

    def fake_fetch(path_and_query: str) -> bytes:
        seen.append(path_and_query)
        if "format=JSON" in path_and_query:
            return b")]}'\n" + b'{"commit":"deadbeef","entries":[]}'
        return base64.b64encode(b"hello")

    monkeypatch.setattr(gerrit_content, "_fetch", fake_fetch)
    monkeypatch.setenv("GERRIT_HTTP_USER", "svc")
    monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "pw")
    get_settings.cache_clear()
    yield seen
    get_settings.cache_clear()


def test_reading_a_markdown_file_uses_show_not_plus(captured):
    gerrit_content.read_text("deadbeef", "skill/rtm-3211/SKILL.md")
    assert captured == ["+show/deadbeef/skill/rtm-3211/SKILL.md?format=TEXT"]
    assert not captured[0].startswith("+/"), "'+/' 对 .md 会答 400"


def test_listing_a_directory_uses_show_and_a_trailing_slash(captured):
    gerrit_content.list_dir("deadbeef", "skill")
    # 末尾斜杠是 Gitiles 区分"列这个目录"和"取这个对象"的开关;少了它拿回的是 commit 信息。
    assert captured == ["+show/deadbeef/skill/?format=JSON"]


def test_the_branch_head_is_read_with_plus_because_it_is_not_an_object(captured):
    assert gerrit_content.head_ref("master") == "deadbeef"
    assert captured == ["+/refs/heads/master?format=JSON"]


def test_the_xssi_prefix_is_stripped(captured):
    """Gitiles 的 JSON 前面有 ``)]}'``,不剥掉就是解析失败。"""
    assert gerrit_content.head_ref("master") == "deadbeef"


def test_a_response_that_is_not_json_says_it_might_be_a_login_page(monkeypatch):
    """最常见的原因是拿到了一页 HTML。说出这一点,不要只报解析失败。"""
    monkeypatch.setattr(gerrit_content, "_fetch", lambda p: b"<!DOCTYPE html>")
    with pytest.raises(gerrit_content.ContentUnavailable) as exc:
        gerrit_content.head_ref("master")
    assert "登录页" in str(exc.value)


def test_without_credentials_it_says_so_instead_of_trying(monkeypatch):
    """凭据缺失与"读不到"分开:前者要去配置,重试永远不会好。"""
    monkeypatch.setenv("GERRIT_HTTP_USER", "")
    monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(gerrit_content.NotConfigured):
            gerrit_content._opener()
    finally:
        get_settings.cache_clear()


def test_the_web_url_uses_the_branch_not_a_commit(monkeypatch):
    """卡片上的链接钉死在某次提交上,会让读者以为那是最新内容。"""
    monkeypatch.setenv("GERRIT_HTTP_USER", "svc")
    monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "pw")
    get_settings.cache_clear()
    try:
        url = gerrit_content.web_url("master", "agent/jesd-agent")
        assert url.endswith("/+/refs/heads/master/agent/jesd-agent")
        assert "deadbeef" not in url
    finally:
        get_settings.cache_clear()
