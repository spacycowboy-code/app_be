"""Which number an activity column sorts on first, and the prose that describes it.

Activity cells read ``2 / 3`` — distinct people on the left, the count on the
right — and the header cycles through both. The first click used to rank by
people, which answers "how far did this spread" when the question actually being
asked of these columns is "who produced how many": the number a reader compares is
the one after the slash. So the cycle now starts on the count.

There is no browser in this suite, so what is pinned here is the decision rather
than the rendered behaviour: which dataset attribute the first click reads, and the
two hint paragraphs that tell the reader the order. Those paragraphs are the part
that rots — the cycle is four states long, nobody can see it from a screenshot, and
a hint that lists them in the old order is worse than no hint. The behaviour itself
was checked by running ``SORT_JS`` against a stub DOM under node: with rows whose
people and count rankings disagree, the four clicks came out count↓, count↑,
people↓, people↑ and then wrapped.
"""
from __future__ import annotations

import re

import pytest

from app.services.usage_report.i18n import _EN, _ZH
from app.services.usage_report.render import SORT_JS


def _use_second_line() -> str:
    """The one line deciding whether a click reads ``data-v2`` (the count)."""
    lines = [ln.strip() for ln in SORT_JS.splitlines() if "useSecond =" in ln]
    assert len(lines) == 1, lines
    return lines[0]


def test_the_first_click_on_an_activity_column_sorts_by_the_count():
    """States 0 and 1 are the count; people follow at 2 and 3.

    ``next < 2`` is the whole assertion: with ``next >= 2`` the same four states
    exist in the opposite order, which is the behaviour this replaced.
    """
    line = _use_second_line()
    assert "next < 2" in line
    assert "next >= 2" not in line


def test_a_plain_column_never_asks_for_a_count_it_has_not_got():
    """``val`` falls back to ``data-v`` silently, so the guard has to be explicit.

    Without ``hasSecond`` here, every name and percentage column would ask for a
    ``data-v2`` on its first click and be carried by that fallback — working by
    accident, and hiding the next mistake that is not so lucky.
    """
    assert "hasSecond && next < 2" in _use_second_line()


@pytest.mark.parametrize(
    "strings,count_word,people_word",
    [(_ZH, "次数", "人数"), (_EN, "times", "people")],
)
def test_the_hint_lists_the_cycle_in_the_order_it_happens(
    strings, count_word, people_word
):
    """A reader who trusts the hint should get the column they were promised."""
    hint = strings["hint_sort"]
    assert hint.index(count_word) < hint.index(people_word)


@pytest.mark.parametrize("strings", [_ZH, _EN])
def test_the_hint_admits_the_bar_is_drawn_from_a_different_number(strings):
    """The bars still scale on people while the first sort is by count.

    Left unsaid, the first click produces a column of bars that visibly do not
    descend with the rows, which reads as a broken sort. Saying which number the
    bar uses is cheaper than re-scaling the bars per sort state, and the sentence
    is what stops the next reader filing it as a bug.
    """
    hint = strings["hint_sort"]
    assert ("底色条按人数画" in hint) or ("bar is drawn from the people count" in hint)
