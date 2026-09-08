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
    # AI & Innovation used to have a group of its own (``ai-team``). It was
    # removed because a team is not a function, and with the longer key gone this
    # falls to its parent unit — which is the documented consequence of dropping
    # a prefix, not an accident.
    ("BNEW RTE RP PS AI AI & Innovation", "product-system"),
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
    # Chengdu is mapped by SUB-TEAM, to the function each one works on. This
    # reverses the arrangement these rows used to assert (everything under one
    # ``cd`` group) — see ``dept_groups`` module docstring for why. The rows stay
    # listed one by one rather than generated because each mapping rests on its
    # own evidence, and a generator would hide the two that rest on the weakest.
    ("BNEW RTE RP CD IVC T1 Team 1", "ivc"),
    ("BNEW RTE RP CD IVC T2 Team 2", "ivc"),
    ("BNEW RTE RP CD IVC2 IVC Team 2", "ivc"),
    ("BNEW RTE RP CD SW1 Software 1", "radio-sw"),
    ("BNEW RTE RP CD SW2 Software 2", "radio-sw"),
    ("BNEW RTE RP CD SW3 Software 3", "radio-sw"),
    ("BNEW RTE RP CD FS Function System", "product-system"),
    ("BNEW RTE RP CD PS Performance System", "product-system"),
    ("BNEW RTE RP CD INT Integration", "radio-design-1"),
    ("BNEW RTE RP CD PA2 Power Amplifier 2", "radio-design-1"),
    # Chengdu sub-teams left unmapped on purpose: no counterpart code under any
    # Beijing unit and no asset of their own to learn from. Asserted as ``None``
    # so that mapping one later is a visible decision, and so that a site-wide
    # ``BNEW RTE RP CD`` key reappearing — which would quietly capture all four —
    # fails here.
    ("BNEW RTE RP CD ET Enclosure&Thermal", None),
    ("BNEW RTE RP CD FIC Filter & IC", None),
    ("BNEW RTE RP CD TRX Transceiver", None),
    ("BNEW RTE RP CD PPD Power PCB Digital", None),
    # Chengdu with no sub-team code at all: nothing to derive a function from, so
    # the upload dialog pre-fills nothing and the person picks.
    ("BNEW RTE RP CD Radio Products Chengdu", None),
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


def test_a_longer_prefix_still_beats_a_shorter_one(monkeypatch):
    """Longest prefix wins — the mechanism, tested directly.

    Production no longer contains a nested pair. ``ai-team`` was the only one
    (``RP PS AI`` inside ``RP PS``) and it is gone, and Chengdu's keys sit one
    level deeper than the unit keys rather than inside them. So the mechanism is
    currently unexercised by real data, which is exactly why it needs a test of
    its own: the next carve-out will depend on it, and by then nothing else would
    have noticed it breaking.

    A temporary key is injected rather than reusing a real department, so this
    keeps testing the resolution rule even after the table changes again.
    """
    monkeypatch.setitem(DEPT_GROUP_MAP, "BNEW RTE RP IVC T9", "qa")
    assert resolve_group("BNEW RTE RP IVC T9 Team 9") == "qa"
    # A sibling under the same parent is untouched by the carve-out.
    assert resolve_group("BNEW RTE RP IVC T8 Team 8") == "ivc"


def test_chengdus_teams_are_matched_at_their_own_depth():
    """``RP CD IVC`` resolves by its own key, never by the shallower ``RP IVC``.

    Both now answer ``ivc``, which is the decision: a market tab names a
    function, and verification work is verification work on either site. This
    test used to assert the opposite (``!= 'ivc'``, because ``cd`` was a site
    group) — the reversal is deliberate and documented in ``dept_groups``.

    The right answer for the wrong reason is still a bug here, so the depth is
    asserted separately from the value: a Chengdu code must resolve through a
    ``BNEW RTE RP CD *`` key, so two sub-teams that map to *different* functions
    prove the codes are being read at the fifth segment rather than the fourth.
    Were the fourth segment being matched, every Chengdu row would collapse onto
    one group and this would fail.
    """
    assert resolve_group("BNEW RTE RP CD IVC T2 Team 2") == "ivc"
    assert resolve_group("BNEW RTE RP CD SW1 Software 1") == "radio-sw"
    assert resolve_group("BNEW RTE RP CD INT Integration") == "radio-design-1"
    # Beijing's own IVC is unaffected and still resolves through its own key.
    assert resolve_group("BNEW RTE RP IVC T2 Team 2") == "ivc"
    # A code that merely starts with a mapped one must not match either.
    assert resolve_group("BNEW RTE RP QAX Something") is None
    assert resolve_group("BNEW RTE RP IVCX Team 9") is None
    assert resolve_group("BNEW RTE RP CDX Something") is None
    assert resolve_group("BNEW RTE RP CD SW1X Software 1X") is None


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


def test_the_two_places_a_group_label_lives_agree():
    """``seed_data.json`` and ``GROUP_CATEGORY_LABELS`` must not drift.

    They exist for two different situations — the seed file builds a fresh
    database, ``ensure_group_categories`` repairs an existing one on deploy — so
    neither can be deleted. What can happen is that one gets a new group and the
    other does not, and then the same slug shows a different tab title depending
    on how the environment was built. That is the kind of difference nobody
    thinks to check.
    """
    from app.seed import _load
    from app.services.dept_groups import GROUP_CATEGORY_LABELS

    seeded = {c["id"]: c["label"] for c in _load()["categories"]}
    disagree = {
        slug: (label, seeded[slug])
        for slug, label in GROUP_CATEGORY_LABELS.items()
        if slug in seeded and seeded[slug] != label
    }
    assert not disagree, f"label differs between seed and code: {disagree}"

    missing_from_seed = sorted(set(GROUP_CATEGORY_LABELS) - set(seeded))
    assert not missing_from_seed, f"in code but not in seed: {missing_from_seed}"
