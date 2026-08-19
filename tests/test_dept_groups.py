"""Department -> portal group mapping.

The cases below are the real ``department`` values in ``rp_ai_portal`` (51
distinct ones over 125 users), because the mapping is only worth anything if it
covers what the directory actually returns. Anything unmapped is asserted to be
unmapped on purpose, so removing a group from the table is a visible decision
rather than a silent fallthrough.
"""
from app.services.dept_groups import DEPT_GROUP_MAP, resolve_group

# (department as LDAP returns it, expected group or None)
REAL_DEPARTMENTS = [
    ("BNEW RTE RP PS AI AI & Innovation", "ai-team"),
    ("BNEW RTE RP PS FS Function System", "product-system"),
    ("BNEW RTE RP PS PS1 Performance System 1", "product-system"),
    ("BNEW RTE RP PS PS2 Performance System 2", "product-system"),
    ("BNEW RTE RP PS PCD Product Cost & Data", "product-system"),
    ("BNEW RTE RP IVC TE Test Environment", "ivc"),
    ("BNEW RTE RP IVC T1 Team 1", "ivc"),
    ("BNEW RTE RP IVC T2 Team 2", "ivc"),
    ("BNEW RTE RP IVC T3 Team 3", "ivc"),
    ("BNEW RTE RP IVC T4 Team 4", "ivc"),
    ("BNEW RTE RP IVC T5 Team 5", "ivc"),
    ("BNEW RTE RP IVC T6 Team 6", "ivc"),
    ("BNEW RTE RP RD1 INT1 Integration 1", "radio-design-1"),
    ("BNEW RTE RP RD1 INT2 Integration 2", "radio-design-1"),
    ("BNEW RTE RP RD1 ALG Algorithm", "radio-design-1"),
    ("BNEW RTE RP RD1 PA1 Power Amplifier 1", "radio-design-1"),
    ("BNEW RTE RP RD1 PA3 Power Amplifier 3", "radio-design-1"),
    ("BNEW RTE RP RD2 DIG Digital", "radio-design-2"),
    ("BNEW RTE RP RD2 DIG Digital HW Design", "radio-design-2"),
    ("BNEW RTE RP RD2 TRX1 Transceiver 1", "radio-design-2"),
    ("BNEW RTE RP RD2 TRX2 Transceiver 2", "radio-design-2"),
    ("BNEW RTE RP RD2 BP1 Board Power 1", "radio-design-2"),
    ("BNEW RTE RP RD2 PPE PCB&PE", "radio-design-2"),
    ("BNEW RTE RP RD3 TIC Thermal&IC", "radio-design-3"),
    ("BNEW RTE RP RD3 ED1 Enclosure 1", "radio-design-3"),
    ("BNEW RTE RP RD3 ED2 Enclosure 2", "radio-design-3"),
    ("BNEW RTE RP RD3 ANT Antenna", "radio-design-3"),
    ("BNEW RTE RP SWD SW1 Software 1", "radio-sw"),
    ("BNEW RTE RP SWD SW2 Software 2", "radio-sw"),
    ("BNEW RTE RP SWD SW3 Software 3", "radio-sw"),
    ("BNEW RTE RP QA Quality Assurance", "qa"),
    ("BNEW RTE RP QA IFQ In-Field Quality", "qa"),
    ("BNEW RTE RP QA RE Reliability", "qa"),
    ("BNEW RTE RP PO POCD Project Office CD", "pmo"),
    # Chengdu: no portal category represents it, by decision. Its own IVC / PS
    # teams must NOT be folded into the portal's IVC / PS groups.
    ("BNEW RTE RP CD Radio Products Chengdu", None),
    ("BNEW RTE RP CD PS Performance System", None),
    ("BNEW RTE RP CD FS Function System", None),
    ("BNEW RTE RP CD SW1 Software 1", None),
    ("BNEW RTE RP CD SW2 Software 2", None),
    ("BNEW RTE RP CD SW3 Software 3", None),
    ("BNEW RTE RP CD INT Integration", None),
    ("BNEW RTE RP CD TRX Transceiver", None),
    ("BNEW RTE RP CD PPD Power PCB Digital", None),
    ("BNEW RTE RP CD IVC T1 Team 1", None),
    ("BNEW RTE RP CD IVC T2 Team 2", None),
    # Outside Radio Products entirely.
    ("BNEW RTE TSM RAF CN China", None),
    ("BNEW RTE TSM ALG MOD Digtal Modules BJ", None),
    ("BNEW RTE EEE GMT Global HW Meth & Tools", None),
    ("BNEW RTE EEE SP Syst & programs", None),
    ("BNEW RCE CCI CEE HWD HW4 HW Design 4", None),
]


def test_every_real_department_resolves_as_decided():
    wrong = [
        (dept, expected, resolve_group(dept))
        for dept, expected in REAL_DEPARTMENTS
        if resolve_group(dept) != expected
    ]
    assert not wrong, f"mapping changed for: {wrong}"


def test_a_carved_out_team_beats_its_parent_unit():
    """Longest prefix wins, so one team can leave its parent's group."""
    assert resolve_group("BNEW RTE RP PS AI AI & Innovation") == "ai-team"
    assert resolve_group("BNEW RTE RP PS FS Function System") == "product-system"


def test_matching_is_per_segment_not_substring():
    """``RP CD IVC`` is Chengdu's IVC, not the portal's — depth decides."""
    assert resolve_group("BNEW RTE RP CD IVC T2 Team 2") is None
    # A code that merely starts with a mapped one must not match either.
    assert resolve_group("BNEW RTE RP QAX Something") is None
    assert resolve_group("BNEW RTE RP IVCX Team 9") is None


def test_truncation_at_the_column_width_still_resolves():
    """``rp_ai_portal.department`` is String(64): the label is cut, not the codes."""
    long_dept = "BNEW RTE RP RD3 TIC Thermal&IC and a very long trailing label here"
    assert resolve_group(long_dept[:64]) == "radio-design-3"


def test_missing_or_unknown_department_has_no_group():
    for value in (None, "", "   ", "SOMETHING ELSE ENTIRELY"):
        assert resolve_group(value) is None


def test_case_and_spacing_are_normalised():
    assert resolve_group("bnew rte rp ivc te Test Environment") == "ivc"
    assert resolve_group("BNEW  RTE   RP\tIVC TE") == "ivc"


def test_the_table_only_points_at_real_categories():
    """A slug that no category row uses would filter to an empty page."""
    from app.seed import _load

    seeded = {c["id"] for c in _load()["categories"]}
    unknown = {slug for slug in DEPT_GROUP_MAP.values() if slug not in seeded}
    assert not unknown, f"mapped to categories that do not exist: {unknown}"
