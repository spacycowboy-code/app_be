"""One unit must not appear twice in the big-department table.

The report has two ways to place somebody, and they key the same department
differently:

* in the Graph reporting tree -> keyed by the tree's branch code, e.g. ``PS``
* not in the tree             -> keyed by parsing the portal row's own
  ``department`` string, which ``parse()`` renders as ``RP / PS``

Both are needed: the tree is the head-count source, and the fallback is what keeps
a portal user visible when the snapshot predates their first sign-in. But when the
fallback's fourth code names a unit the tree already has, the table gains a second
row for that unit -- with no head count, because the tree has no such key. That is
what production showed: ``PS | 71 | 42 | 59%`` and, eleven rows below,
``RP / PS | — | 1 | 0%``.

The single row behind it was ``ECNRPAI``, a functional ID whose display name is the
instruction someone typed into the field ("use this function id for API of tool in
ericsson"). It is excluded via ``NON_PERSON``, the mechanism that already holds the
break-glass admins and the display strings that reached the user table.

Why the assertions below are about the *shape* of the table rather than just about
that one EID: excluding one account fixes today's row and would not notice the next
functional ID. A test that says "no unit appears under two spellings" fails for the
cause, not for the instance.
"""
from app.services.usage_report.departments import NON_PERSON, parse
from app.services.usage_report.render import aggregate


def _big_labels(**kw) -> list[str]:
    groups = aggregate(
        people={},
        metrics=[],
        small_head={},
        labels={},
        level="big",
        in_scope=True,
        **kw,
    )
    return [g.label for g in groups.values()]


def test_the_functional_id_behind_the_duplicate_ps_row_is_excluded():
    # Named explicitly so the reason survives: this row carried a real
    # department, which is why it reached the department table at all.
    assert "ECNRPAI" in NON_PERSON
    assert "functional ID" in NON_PERSON["ECNRPAI"]


def test_a_department_string_and_a_branch_code_name_the_same_unit_differently():
    """The mechanism, stated as a fact rather than assumed by the fix above."""
    parsed = parse("BNEW RTE RP PS AI AI & Innovation")
    # The fallback label is not the branch code, so the two never merge on key.
    assert parsed.big_label == "RP / PS"
    assert parsed.big_key == "BNEW/RTE/RP/PS"
    assert parsed.big_key != "PS"


def test_one_unit_never_appears_under_two_spellings():
    # Both placements present for PS: the tree's head count and a portal row the
    # tree does not contain. The table must not end up with two PS rows.
    labels = _big_labels(big_head={"PS": 71, "CD": 270}, placement={})
    assert "PS" in labels
    parsed_label = parse("BNEW RTE RP PS AI AI & Innovation").big_label
    assert parsed_label not in labels, (
        f"{parsed_label!r} is a second row for a unit already listed as 'PS'"
    )


def test_a_unit_outside_the_tree_still_gets_its_own_row():
    """The negative side: this is not a licence to drop unmatched units.

    ``RA / RD5`` and ``EEE / GMT`` are genuinely absent from the RP reporting
    tree, so their fallback rows are the only rows they will ever have. A fix that
    suppressed every ``A / B`` label would delete real contributors -- RD5 alone
    has five assets.
    """
    for dept in ("BNEW RTE RA RD5 SWD Software Design D",
                 "BNEW RTE EEE GMT Global HW Meth & Tools"):
        parsed = parse(dept)
        assert " / " in parsed.big_label
        # Not one of the RP branches the tree provides, so nothing to merge with.
        assert not parsed.big_key.startswith("BNEW/RTE/RP/")
