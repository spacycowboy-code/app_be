"""One unit must not appear twice in the big-department table.

Production showed Product System twice: the real ``PS | 71 | 42 | 59%`` and,
eleven rows below, ``RP / PS | — | 1 | 0%`` with no head count.

The single row behind it was ``ECNRPAI`` -- **this service's own LDAP bind
account**. ``build_people`` already has a guard for exactly that account, and the
guard's own docstring says it is "excluded by matching configuration rather than by
name". It never fired: the setting is a UPN (``ecnrpai@ericsson.se``) while the
EIDs it is compared against are bare and upper case (``ECNRPAI``), so the equality
was always false.

With the guard dead, the account fell through to the weakest placement rule -- has
a name and a department, so treat it as a person -- which keys the group from the
parsed department string (``BNEW/RTE/RP/PS``, labelled ``RP / PS``) rather than
from the reporting tree's branch code (``PS``). Two keys, one unit, two rows.

Why the tests below are not about that one EID: excluding one account by name fixes
today's row and would miss the next one. The bug is a comparison that cannot
match, so that is what gets pinned -- both directions, because a "fix" that
stripped too much would start excluding real people.
"""
from app.services.usage_report.departments import parse
from app.services.usage_report.render import aggregate, bind_eid, build_people


def test_a_upn_shaped_setting_reduces_to_the_bare_eid():
    # The production value. Before the fix this stayed 'ECNRPAI@ERICSSON.SE' and
    # never equalled any portal EID.
    assert bind_eid("ecnrpai@ericsson.se") == "ECNRPAI"


def test_an_already_bare_setting_still_works():
    # The other supported shape must not regress; both forms appear in the wild.
    assert bind_eid("ECNRPAI") == "ECNRPAI"
    assert bind_eid("  ecnrpai  ") == "ECNRPAI"


def test_no_bind_account_configured_excludes_nobody():
    # An empty setting must not turn into a value that matches something. EIDs are
    # never empty by the time the comparison runs, so "" is the safe answer.
    assert bind_eid("") == ""
    assert bind_eid(None) == ""


def test_a_department_string_and_a_branch_code_name_the_same_unit_differently():
    """The mechanism behind the duplicate row, stated rather than assumed."""
    parsed = parse("BNEW RTE RP PS AI AI & Innovation")
    assert parsed.big_label == "RP / PS"
    assert parsed.big_key == "BNEW/RTE/RP/PS"
    assert parsed.big_key != "PS"


def test_one_unit_never_appears_under_two_spellings():
    groups = aggregate(
        people={},
        metrics=[],
        big_head={"PS": 71, "CD": 270},
        small_head={},
        placement={},
        labels={},
        level="big",
        in_scope=True,
    )
    labels = [g.label for g in groups.values()]
    assert "PS" in labels
    parsed_label = parse("BNEW RTE RP PS AI AI & Innovation").big_label
    assert parsed_label not in labels, (
        f"{parsed_label!r} is a second row for a unit already listed as 'PS'"
    )


def test_build_people_actually_excludes_the_bind_account():
    """Covers the wiring, not just the helper.

    The helper can be correct while nothing calls it -- which is precisely how this
    bug existed: the guard was there, the intent was documented, and the comparison
    could not match. A probe that reverted only the call site left every
    helper-level test green, so this test exercises ``build_people`` itself.
    """
    from sqlalchemy import create_engine, text as sa_text

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa_text(
            "create table rp_ai_portal (eid text, display_name text, "
            "department text, origin text)"
        ))
        conn.execute(sa_text(
            "insert into rp_ai_portal values "
            "('ECNRPAI', 'use this function id for API of tool in ericsson', "
            "'BNEW RTE RP PS AI AI & Innovation', 'login'), "
            "('EZHGMIO', 'Miao Zhang M', 'BNEW RTE RP PS AI AI & Innovation', 'login')"
        ))
    with engine.connect() as conn:
        people, excluded, _ = build_people(
            conn, {"people": {}}, {}, "ecnrpai@ericsson.se"
        )

    assert "ECNRPAI" not in people, "服务自己的目录绑定账号被当成了人"
    assert "ECNRPAI" in excluded
    # The real colleague in the same department must still be a person, so the
    # exclusion is targeted rather than "anything in AI & Innovation".
    assert "EZHGMIO" in people


def test_a_unit_outside_the_tree_still_gets_its_own_row():
    """The negative side: this is not a licence to drop unmatched units.

    ``RA / RD5`` and ``EEE / GMT`` are genuinely absent from the RP reporting
    tree, so their fallback rows are the only rows they will ever have -- RD5 alone
    has five assets. A fix that suppressed every ``A / B`` label, or that widened
    the bind-account match, would delete real contributors.
    """
    for dept in ("BNEW RTE RA RD5 SWD Software Design D",
                 "BNEW RTE EEE GMT Global HW Meth & Tools"):
        parsed = parse(dept)
        assert " / " in parsed.big_label
        assert not parsed.big_key.startswith("BNEW/RTE/RP/")
        # And such a person must never be mistaken for the bind account.
        assert bind_eid("ecnrpai@ericsson.se") != parsed.big_key
