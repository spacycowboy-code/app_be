"""The card group label, pinned to the 53 sub-units that exist in the org.

The expected values are the list supplied by the owner of this feature — the
full enumeration of Radio Products' sub-units, each labelled with the office,
the big department and the small department as they should appear on a card.
They are the specification, so they are asserted verbatim rather than
re-derived: a rule change that "improves" one of these rows has to fail here and
be argued for.

Department strings for sub-units that appear in ``rp_ai_portal`` today are the
real values (see ``test_dept_groups.REAL_DEPARTMENTS``). The rest carry an
illustrative trailing label, because only the leading run of codes is
load-bearing and where that run stops is already covered by the department
parser's own tests.
"""
import pytest

from app.services.card_group import CardGroup, group_for_department

# (department as LDAP returns it, the label the card must show)
SUB_UNITS = [
    # --- Beijing: the fourth code is the big department -------------------
    ("BNEW RTE RP IVC TE Test Environment", "BJ IVC TE"),
    ("BNEW RTE RP IVC T1 Team 1", "BJ IVC T1"),
    ("BNEW RTE RP IVC T2 Team 2", "BJ IVC T2"),
    ("BNEW RTE RP IVC T3 Team 3", "BJ IVC T3"),
    ("BNEW RTE RP IVC T4 Team 4", "BJ IVC T4"),
    ("BNEW RTE RP IVC T5 Team 5", "BJ IVC T5"),
    ("BNEW RTE RP IVC T6 Team 6", "BJ IVC T6"),
    ("BNEW RTE RP PS AI AI & Innovation", "BJ PS AI"),
    ("BNEW RTE RP PS FS Function System", "BJ PS FS"),
    ("BNEW RTE RP PS PS1 Performance System 1", "BJ PS PS1"),
    ("BNEW RTE RP PS PS2 Performance System 2", "BJ PS PS2"),
    ("BNEW RTE RP PS PCD Product Cost & Data", "BJ PS PCD"),
    ("BNEW RTE RP RD1 ALG Algorithm", "BJ RD1 ALG"),
    ("BNEW RTE RP RD1 INT1 Integration 1", "BJ RD1 INT1"),
    ("BNEW RTE RP RD1 INT2 Integration 2", "BJ RD1 INT2"),
    ("BNEW RTE RP RD1 PA1 Power Amplifier 1", "BJ RD1 PA1"),
    ("BNEW RTE RP RD1 PA2 Power Amplifier 2", "BJ RD1 PA2"),
    ("BNEW RTE RP RD1 PA3 Power Amplifier 3", "BJ RD1 PA3"),
    ("BNEW RTE RP RD2 DIG Digital", "BJ RD2 DIG"),
    ("BNEW RTE RP RD2 TRX1 Transceiver 1", "BJ RD2 TRX1"),
    ("BNEW RTE RP RD2 TRX2 Transceiver 2", "BJ RD2 TRX2"),
    ("BNEW RTE RP RD2 BP1 Board Power 1", "BJ RD2 BP1"),
    ("BNEW RTE RP RD2 BP2 Board Power 2", "BJ RD2 BP2"),
    ("BNEW RTE RP RD2 PPE PCB&PE", "BJ RD2 PPE"),
    ("BNEW RTE RP RD3 ANT Antenna", "BJ RD3 ANT"),
    ("BNEW RTE RP RD3 ED1 Enclosure 1", "BJ RD3 ED1"),
    ("BNEW RTE RP RD3 ED2 Enclosure 2", "BJ RD3 ED2"),
    ("BNEW RTE RP RD3 FU Filter Unit", "BJ RD3 FU"),
    ("BNEW RTE RP RD3 TIC Thermal&IC", "BJ RD3 TIC"),
    ("BNEW RTE RP SWD SW1 Software 1", "BJ SWD SW1"),
    ("BNEW RTE RP SWD SW2 Software 2", "BJ SWD SW2"),
    ("BNEW RTE RP SWD SW3 Software 3", "BJ SWD SW3"),
    ("BNEW RTE RP SWD SW4 Software 4", "BJ SWD SW4"),
    ("BNEW RTE RP QA IFQ In-Field Quality", "BJ QA IFQ"),
    ("BNEW RTE RP QA RE Reliability", "BJ QA RE"),
    ("BNEW RTE RP PO OP Operations", "BJ PO OP"),
    ("BNEW RTE RP PO POBJ1 Project Office BJ 1", "BJ PO POBJ1"),
    ("BNEW RTE RP PO POBJ2 Project Office BJ 2", "BJ PO POBJ2"),
    # Named for Chengdu, but a team of Beijing's project office.
    ("BNEW RTE RP PO POCD Project Office CD", "BJ PO POCD"),
    # --- Chengdu: the fourth code names the site, so departments -----------
    # --- start one level deeper, and most have no sub-unit -----------------
    ("BNEW RTE RP CD ET Equipment Test", "CD ET"),
    ("BNEW RTE RP CD FIC Filter&IC", "CD FIC"),
    ("BNEW RTE RP CD FS Function System", "CD FS"),
    ("BNEW RTE RP CD PA1 Power Amplifier 1", "CD PA1"),
    ("BNEW RTE RP CD PA2 Power Amplifier 2", "CD PA2"),
    ("BNEW RTE RP CD PPD Power PCB Digital", "CD PPD"),
    ("BNEW RTE RP CD SW1 Software 1", "CD SW1"),
    ("BNEW RTE RP CD SW2 Software 2", "CD SW2"),
    ("BNEW RTE RP CD SW3 Software 3", "CD SW3"),
    ("BNEW RTE RP CD TRX Transceiver", "CD TRX"),
    # The one Chengdu unit with teams under it.
    ("BNEW RTE RP CD IVC T1 Team 1", "CD IVC T1"),
    ("BNEW RTE RP CD IVC T2 Team 2", "CD IVC T2"),
    # --- The two exceptions: Chengdu-org units listed as Beijing-located ---
    ("BNEW RTE RP CD PS Performance System", "BJ CD PS"),
    ("BNEW RTE RP CD INT Integration", "BJ CD INT"),
]


def test_every_sub_unit_gets_the_label_it_was_specified_with():
    wrong = [
        (dept, expected, got.label if got else None)
        for dept, expected in SUB_UNITS
        if (got := group_for_department(dept)) is None or got.label != expected
    ]
    assert not wrong, f"label changed for: {wrong}"


def test_the_list_covers_every_sub_unit_exactly_once():
    """53 rows, no duplicates — the supplied enumeration, unabridged."""
    assert len(SUB_UNITS) == 53
    assert len({label for _, label in SUB_UNITS}) == 53


@pytest.mark.parametrize(
    "department, site, big, small",
    [
        ("BNEW RTE RP IVC TE Test Environment", "BJ", "IVC", "TE"),
        # Flat Chengdu unit: no sub-unit exists, so the third part is empty
        # rather than borrowed from somewhere else.
        ("BNEW RTE RP CD PPD Power PCB Digital", "CD", "PPD", None),
        ("BNEW RTE RP CD IVC T2 Team 2", "CD", "IVC", "T2"),
        # Supplied verbatim: CD stays the big department, the sub-unit is
        # demoted, and the site is the one the path cannot know.
        ("BNEW RTE RP CD PS Performance System", "BJ", "CD", "PS"),
    ],
)
def test_the_three_parts_are_split_where_the_card_expects_them(
    department, site, big, small
):
    group = group_for_department(department)
    assert group == CardGroup(site, big, small, department)


def test_a_department_with_no_sub_unit_still_places_the_person():
    """Two parts is a real answer; a person at their unit's own level has no team."""
    group = group_for_department("BNEW RTE RP QA Quality Assurance")
    assert group is not None
    assert (group.site, group.big_dept, group.small_dept) == ("BJ", "QA", None)
    assert group.label == "BJ QA"


def test_the_chengdu_site_level_is_a_site_and_nothing_more():
    """``RP CD`` on its own names the office, not a department in it."""
    group = group_for_department("BNEW RTE RP CD Radio Products Chengdu")
    assert group is not None
    assert (group.site, group.big_dept, group.small_dept) == ("CD", None, None)
    assert group.label == "CD"


@pytest.mark.parametrize(
    "department",
    [
        # Outside Radio Products: the BJ/CD split says nothing true about them,
        # and a guess would put an EEE employee in an office they never sat in.
        "BNEW RTE TSM RAF CN China",
        "BNEW RTE TSM ALG MOD Digtal Modules BJ",
        "BNEW RTE EEE GMT Global HW Meth & Tools",
        "BNEW RTE EEE SP Syst & programs",
        "BNEW RCE CCI CEE HWD HW4 HW Design 4",
        # No codes at all, or nothing to place.
        "Executive Management Ericsson",
        "SOMETHING ELSE ENTIRELY",
        None,
        "",
        "   ",
    ],
)
def test_an_unplaceable_department_shows_no_group_at_all(department):
    assert group_for_department(department) is None


def test_a_code_that_merely_starts_with_cd_is_not_chengdu():
    """Matching is per segment: ``CDX`` is a department, not the Chengdu site."""
    group = group_for_department("BNEW RTE RP CDX Something Else")
    assert group is not None
    assert (group.site, group.big_dept) == ("BJ", "CDX")


def test_truncation_at_the_column_width_still_places_the_person():
    """``rp_ai_portal.department`` is String(64): the label is cut, not the codes."""
    long_dept = "BNEW RTE RP RD3 TIC Thermal&IC and a very long trailing label here"
    group = group_for_department(long_dept[:64])
    assert group is not None
    assert group.label == "BJ RD3 TIC"


def test_the_raw_path_is_carried_for_the_tooltip():
    """The card explains a three-letter code by showing where it came from."""
    group = group_for_department("BNEW RTE RP IVC TE Test Environment")
    assert group is not None
    assert group.department == "BNEW RTE RP IVC TE Test Environment"
