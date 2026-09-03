"""What the monthly report calls each big department.

The org chart and the portal disagree on two names. The chart's fourth code is
``PO`` for the group the portal files under ``pmo`` / "PMO", and the portal's
group label for ``QA`` is now "Q&A". The report reads the chart, so without a
display step the same person sees two names for one department depending on which
page they opened.

The invariant worth pinning is the split: **the key is the chart's code and only
the label changes.** Renaming the key instead would silently break every join in
the report (``big_head``, ``placement``, ``Group.key``) and the symptom would be a
department losing its numbers rather than an error.
"""
from app.services.usage_report.departments import (
    BRANCH_DISPLAY,
    UNKNOWN,
    branch_display,
)
from app.services.usage_report.render import aggregate, esc


def test_the_two_codes_the_portal_spells_differently_are_mapped():
    assert branch_display("PO") == "PMO"
    assert branch_display("QA") == "Q&A"


def test_every_other_code_is_left_exactly_as_the_org_chart_wrote_it():
    # Negative side of the same rule: the map is short on purpose, so anything
    # not listed must pass through untouched rather than be invented.
    for code in ("CD", "IVC", "RD1", "RD2", "RD3", "PS", "SWD"):
        assert branch_display(code) == code


def test_swd_is_deliberately_not_mapped():
    """Documents a decision, not an oversight.

    The portal calls that group "Radio SW" with the short code ``RSW``, so a
    mapping here would introduce a third spelling instead of reconciling two.
    """
    assert "SWD" not in BRANCH_DISPLAY


def test_a_missing_department_still_renders():
    # ``branch`` is empty for people the tree walk could not place, and the report
    # substitutes UNKNOWN. Neither may blow up or turn into the string "None".
    assert branch_display(UNKNOWN) == UNKNOWN
    assert branch_display("") == ""
    assert branch_display(None) == ""


def test_the_group_keeps_the_org_chart_code_as_its_key_and_shows_the_new_label():
    groups = aggregate(
        people={},
        metrics=[],
        big_head={"PO": 6, "QA": 3, "RD1": 4},
        small_head={},
        placement={},
        labels={},
        level="big",
        in_scope=True,
    )
    # Keys are untouched — this is what every other part of the report joins on.
    assert sorted(groups) == ["PO", "QA", "RD1"]
    assert {k: g.label for k, g in groups.items()} == {
        "PO": "PMO",
        "QA": "Q&A",
        "RD1": "RD1",
    }
    # And the head counts still landed on the right rows.
    assert {k: g.real for k, g in groups.items()} == {"PO": 6, "QA": 3, "RD1": 4}


def test_the_ampersand_is_escaped_when_the_label_reaches_the_page():
    """The report builds HTML with f-strings, so this is the thing that saves it.

    ``table()`` puts every label through ``esc``; if that ever stops being true,
    "Q&A" goes into the page as a bare ``&``.
    """
    assert esc(branch_display("QA")) == "Q&amp;A"
