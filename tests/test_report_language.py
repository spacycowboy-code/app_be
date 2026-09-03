"""The report's two languages, and what is deliberately *not* translated.

Scope was a decision, not an omission: skeleton only. Headings, KPI labels, table
headers, column names, metric names and units are translated; the ``口径``
paragraphs stay Chinese in both languages. The tests below pin both halves of
that, because a later reader cannot tell "we chose to stop here" from "somebody
forgot these" by looking at the page.

The two structural traps this file exists for:

* a key present in one language and missing in the other — ``Strings.__getitem__``
  raises, so the English page would 500 rather than degrade, and only on the
  branch that renders that one cell;
* a ``{placeholder}`` dropped or renamed in one language — a dropped one is a
  ``KeyError`` at render time, an added one prints a literal brace.

Both are invisible to a test that only renders Chinese.
"""
import re

import pytest

from app.services.usage_report.i18n import (
    DEFAULT_LANG,
    LANGS,
    _EN,
    _METRIC_EN,
    _ZH,
    Strings,
    strings,
)

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_the_two_languages_carry_exactly_the_same_keys():
    missing_en = sorted(set(_ZH) - set(_EN))
    missing_zh = sorted(set(_EN) - set(_ZH))
    assert not missing_en, f"英文表缺这些键：{missing_en}"
    assert not missing_zh, f"中文表缺这些键：{missing_zh}"


@pytest.mark.parametrize("key", sorted(_ZH))
def test_each_key_uses_the_same_placeholders_in_both_languages(key):
    assert set(_PLACEHOLDER.findall(_ZH[key])) == set(_PLACEHOLDER.findall(_EN[key])), (
        f"{key}: 两种语言的占位符对不上 —— 少一个是渲染时 KeyError，"
        f"多一个是页面上多出一对花括号"
    )


def test_no_value_is_empty():
    for table_name, table in (("zh", _ZH), ("en", _EN)):
        blank = sorted(k for k, v in table.items() if not v.strip())
        assert not blank, f"{table_name} 表里这些键是空的：{blank}"


def test_the_english_table_has_no_chinese_left_in_it():
    """Catches a key that was copied across and never actually translated."""
    zh_char = re.compile(r"[\u4e00-\u9fff]")
    leftover = sorted(k for k, v in _EN.items() if zh_char.search(v))
    assert not leftover, f"英文表里这些值还带中文：{leftover}"


def test_metric_names_fall_back_to_chinese_rather_than_to_the_key():
    """An untranslated metric shows its Chinese name, never ``skill_upload``."""
    s = strings("en")
    assert s.metric_label("skill_upload", "上传 skill") == "Skills uploaded"
    # A metric nobody has added to the table yet:
    assert s.metric_label("no_such_metric", "某个新指标") == "某个新指标"
    assert s.metric_unit("no_such_metric", "个") == "个"


def test_every_translated_metric_has_both_a_name_and_a_unit():
    for key, pair in _METRIC_EN.items():
        assert len(pair) == 2, key
        assert all(v and v.strip() for v in pair), key


def test_chinese_is_the_default_so_an_old_bookmark_keeps_working():
    assert DEFAULT_LANG == "zh"
    assert strings(None).lang == "zh"
    assert strings(None)["h1"] == _ZH["h1"]


def test_an_unknown_language_is_refused_not_guessed():
    with pytest.raises(ValueError):
        Strings("eng")
    with pytest.raises(ValueError):
        Strings("EN")


def test_a_missing_key_raises_instead_of_printing_itself():
    with pytest.raises(KeyError):
        strings("en")["not_a_key"]


def test_langs_is_exactly_the_two_we_support():
    assert LANGS == ("zh", "en")
