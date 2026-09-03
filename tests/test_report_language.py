"""The report's two languages.

``lang=en`` translates the **whole** page: headings, KPI labels, table headers,
column names, metric names and units, and the ``口径`` paragraphs. An earlier round
stopped at the skeleton and left the prose Chinese; these tests were written for
that scope and have been widened, because the interesting property is now "no half
-translated page" rather than "translated exactly this far".

The three structural traps this file exists for:

* a key present in one language and missing in the other — ``Strings.__getitem__``
  raises, so the English page would 500 rather than degrade, and only on the
  branch that renders that one cell;
* a ``{placeholder}`` dropped or renamed in one language — a dropped one is a
  ``KeyError`` at render time, an added one prints a literal brace;
* a metric table keyed by a name no metric actually has — this happened: two
  entries read ``market_upload`` and ``tool_run`` against the real
  ``market_item_upload`` and ``usage_event``, and because a missing key falls back
  to Chinese, those two appendix entries were silently Chinese on the English page
  and every test still passed.

All three are invisible to a test that only renders Chinese.
"""
import re

import pytest

from app.database import SessionLocal
from app.services.usage_report import figures
from app.services.usage_report.i18n import (
    DEFAULT_LANG,
    LANGS,
    _EN,
    _METRIC_EN,
    _METRIC_NOTE_EN,
    _METRIC_UNATTR_EN,
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


# --- the metric tables -----------------------------------------------------
#
# ``metric_label`` / ``metric_unit`` / ``metric_note`` all fall back to the Chinese
# when a key is absent. That fallback is deliberate — an untranslated metric should
# read oddly rather than lose the paragraph that says what its number includes — but
# it is also what let two misspelled keys sit unnoticed. These tests make the table
# answer to the real metric list instead of to itself.


def _metrics() -> list:
    """The real metric list. Opens its own session, as the sibling tests do."""
    with SessionLocal() as db:
        return figures.collect(db.connection())


def _real_metric_keys() -> set[str]:
    return {m.key for m in _metrics()}


def test_every_key_in_the_metric_tables_is_a_real_metric():
    """A key no metric has is dead weight that reads as coverage.

    This is the assertion that was missing: ``market_upload`` and ``tool_run`` were
    entries for metrics called ``market_item_upload`` and ``usage_event``, so the
    English page fell back to Chinese for both and nothing complained.
    """
    real = _real_metric_keys()
    for name, table in (
        ("_METRIC_EN", _METRIC_EN),
        ("_METRIC_NOTE_EN", _METRIC_NOTE_EN),
        ("_METRIC_UNATTR_EN", _METRIC_UNATTR_EN),
    ):
        strays = sorted(set(table) - real)
        assert not strays, f"{name} 里这些键不是真实指标：{strays}"


def test_every_metric_has_an_english_name_and_unit():
    missing = sorted(_real_metric_keys() - set(_METRIC_EN))
    assert not missing, f"这些指标没有英文名：{missing}"


def test_every_metric_has_an_english_note():
    """The note is the metric's 口径 — the part a reader needs most."""
    missing = sorted(_real_metric_keys() - set(_METRIC_NOTE_EN))
    assert not missing, f"这些指标的口径没有英文：{missing}"


def test_every_metric_with_an_unattributed_note_has_an_english_one():
    metrics = _metrics()
    missing = sorted(
        m.key for m in metrics
        if m.unattributed_note and m.key not in _METRIC_UNATTR_EN
    )
    assert not missing, f"这些指标的「无法归属」说明没有英文：{missing}"


def test_the_english_metric_tables_have_no_chinese_left_in_them():
    for name, values in (
        ("_METRIC_EN", [v for pair in _METRIC_EN.values() for v in pair]),
        ("_METRIC_NOTE_EN", list(_METRIC_NOTE_EN.values())),
        ("_METRIC_UNATTR_EN", list(_METRIC_UNATTR_EN.values())),
    ):
        bad = [v for v in values if re.search(r"[\u4e00-\u9fff]", v)]
        assert not bad, f"{name} 里还有中文：{bad}"


def test_an_empty_english_note_is_only_allowed_where_the_chinese_is_empty():
    """``market_favorite`` genuinely has no note; nothing else may be blank.

    Without this, deleting a translation would look the same as a metric that
    never had a note.
    """
    zh_notes = {m.key: m.note for m in _metrics()}
    blank_en = {k for k, v in _METRIC_NOTE_EN.items() if not v.strip()}
    for key in sorted(blank_en):
        assert not zh_notes.get(key, "").strip(), (
            f"{key} 的中文口径有内容，英文却是空的"
        )


def test_metric_notes_fall_back_to_chinese_rather_than_to_nothing():
    en = strings("en")
    assert en.metric_note("no_such_metric", "中文口径") == "中文口径"
    assert en.metric_unattr("no_such_metric", "中文说明") == "中文说明"


# --- number agreement ------------------------------------------------------
#
# English inflects and Chinese does not, which only shows up once the prose is
# translated: the first render said "These 1 teams" and "Another 1 people". The
# forms live in the table as ``singular|plural`` so a noun cannot go singular in
# one language and plural in the other.

_CNT_KEYS = sorted(k for k in _ZH if k.startswith("cnt_"))


def test_there_are_count_keys_at_all():
    """Guards the tests below, which would all pass vacuously on an empty list."""
    assert _CNT_KEYS


@pytest.mark.parametrize("key", _CNT_KEYS)
def test_every_count_key_offers_exactly_two_forms(key):
    for name, table in (("zh", _ZH), ("en", _EN)):
        forms = table[key].split("|")
        assert len(forms) == 2, f"{name}/{key} 应当是 单数|复数 两段，实际 {forms}"
        for form in forms:
            assert "{n}" in form, f"{name}/{key} 的 {form!r} 少了 {{n}}"


@pytest.mark.parametrize("key", _CNT_KEYS)
def test_chinese_does_not_inflect(key):
    """Both Chinese forms identical, so ``count`` cannot change Chinese wording."""
    one, many = _ZH[key].split("|")
    assert one == many, f"中文 {key} 的两段不一致：{one!r} / {many!r}"


@pytest.mark.parametrize("key", _CNT_KEYS)
def test_english_singular_and_plural_actually_differ(key):
    one, many = _EN[key].split("|")
    assert one != many, f"英文 {key} 的单复数一样，那它不需要是 cnt_ 键：{one!r}"


def test_count_picks_the_singular_only_for_one():
    en = strings("en")
    assert en.count("cnt_people", 1) == "1 person"
    assert en.count("cnt_people", 2) == "2 people"
    assert en.count("cnt_people", 0) == "0 people"
    zh = strings("zh")
    assert zh.count("cnt_people", 1) == "1 人"
    assert zh.count("cnt_people", 2) == "2 人"


def test_the_english_page_says_one_team_not_one_teams(client=None):
    """The exact wording the first render got wrong, pinned at the unit level."""
    en = strings("en")
    assert en.count("cnt_teams_group", 1) == "1 <b>team</b>"
    assert en.count("cnt_teams_bare", 1) == "1 team"
    assert en.count("cnt_accounts", 1) == "1 non-personal account"
    assert en.count("cnt_rows", 1) == "1 row"


# --- whole-sentence agreement ----------------------------------------------

#: Keys read through ``pick`` rather than ``fmt``. Listed here so the tests below
#: fail when a sentence is added to that call path without an English singular.
_PICKED = ("lead_used_vs_recent", "lead_zero_1", "note_zero_leads", "lead_outside")


@pytest.mark.parametrize("key", _PICKED)
def test_the_picked_sentences_have_an_english_singular(key):
    """Agreement in English reaches the verb, not just the noun.

    The first translated render read "Another 1 person **have** not signed in" and
    "These 1 team **hold**" — the noun was fixed and the verb was not.
    """
    forms = _EN[key].split("|")
    assert len(forms) == 2, f"英文 {key} 需要 单数|复数 两句，实际 {len(forms)} 句"
    assert forms[0] != forms[1], f"英文 {key} 的两句一样，那它不必走 pick"


@pytest.mark.parametrize("key", _PICKED)
def test_chinese_keeps_one_form_for_the_picked_sentences(key):
    """Chinese does not inflect, so a second copy would only be a thing to forget."""
    assert "|" not in _ZH[key], f"中文 {key} 不该有两份"


@pytest.mark.parametrize("key", _PICKED)
def test_both_english_forms_use_the_same_placeholders(key):
    one, many = _EN[key].split("|")
    assert set(_PLACEHOLDER.findall(one)) == set(_PLACEHOLDER.findall(many)), (
        f"英文 {key} 单复数两句的占位符不一致"
    )


def test_pick_uses_the_singular_only_for_one():
    en = strings("en")
    one = en.pick("lead_outside", 1, outside="1 person", scope="RP")
    many = en.pick("lead_outside", 2, outside="2 people", scope="RP")
    assert "This 1 person is from another organisation" in one
    assert "These 2 people are from other organisations" in many


def test_pick_falls_back_to_the_single_form_when_there_is_only_one():
    """Which is what makes the Chinese side need no duplicate."""
    zh = strings("zh")
    one = zh.pick("lead_outside", 1, outside="1 人", scope="RP")
    many = zh.pick("lead_outside", 2, outside="2 人", scope="RP")
    assert one.replace("1 人", "N") == many.replace("2 人", "N")
