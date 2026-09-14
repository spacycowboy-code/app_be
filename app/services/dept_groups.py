"""Map an LDAP department path to a portal group (a category slug).

LDAP hands us ``department`` as a space-separated path of org codes followed by
the last code's human label::

    BNEW RTE RP IVC TE Test Environment
    ^^^^ ^^^ ^^ ^^^ ^^ ^^^^^^^^^^^^^^^^
    codes, coarse to fine        label of the last code

A group is a **function**, not an organisation
------------------------------------------------

The slugs below read like org units (``IVC``, ``PS``, ``RD1``…) because that is
where they came from, but what they mean on a market tab is the *discipline the
asset is about*: somebody browsing the ``IVC`` tab is looking for verification
tooling, not for Beijing's org chart. Two sites doing the same work belong under
one tab.

That is a deliberate reversal. This table used to send every Chengdu department
to a single ``cd`` group so that a site was never mixed into another site's tab,
and a comment here called filing ``RP CD IVC`` under ``ivc`` a mistake. Under the
current definition it is the correct answer, and ``cd`` was the mistake: it made
the tab list a mix of two incompatible questions ("which function?" for nine
tabs, "which site?" for one), so a Chengdu engineer's verification tool was
reachable from neither the tab naming their discipline nor any list of Chengdu's
own work — it sat in a tab nobody browses by choice.

Chengdu's contribution is not lost by this, which is what made it safe: the
monthly usage report and the Department Details pages both attribute by the
uploader's **LDAP organisation** and still give Chengdu its own row. Those are
separate code paths on purpose (``usage_report/departments.py`` and
``services/departments.py``) — the market answers "what is this?", the report
answers "who wrote it?", and neither answer is a substitute for the other.

Because the groups are functions, a department maps to one by the *finest* code
that carries a functional signal, which is why Chengdu appears below as a set of
sub-team prefixes rather than one site-wide key.

Why prefixes and not whole strings: the full value identifies a team, and there
are 50+ of them in a directory that reorganises. The codes at the front are the
stable part, and a prefix match keeps working when a new team appears under a
known unit. It also survives the ``String(64)`` column: truncation eats the
trailing label, never the leading codes.

Why longest-match-first: it lets one team be filed by its own function without
touching its parent (``RP CD SW1`` -> ``radio-sw`` while ``RP CD IVC`` ->
``ivc``, both under the same site).

Why an unmapped department yields ``None`` rather than *some other department's*
group: a wrong group is worse than no group. The asset would be filed under
someone else's filter, where its uploader will never notice it. ``None`` means
"we do not know", and that is deliberately not the same value as "we know it
belongs nowhere".

``FALLBACK_GROUP_ID`` (``others``) is what "belongs nowhere" resolves to, and it
does not contradict the paragraph above: it claims no department, so it cannot
misattribute one. It exists because ``None`` had a cost that only showed up in
the numbers — an asset with no group is counted in a market's "All" tab and
appears under no department tab, so the tabs stopped adding up to the total, and
the rows doing it were invisible in exactly the view that would have revealed
them. Ten rows had drifted out this way before anyone noticed.

Who applies the fallback matters. The **automatic** importers use it, because
there is no human in the loop to ask and their alternative is a row nobody can
see. The **upload dialogs** do not: a person is right there, ``others`` is a
选项 they can pick, and defaulting for them is how the old ``public`` default
quietly swallowed a year of Chengdu uploads.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.category import RpAiCategory
from app.models.user import RpAiPortal

logger = logging.getLogger(__name__)

#: Categories that exist as rows but are not places to file a skill.
#: ``all`` is a filter; ``official`` is filled by the repo sync and carries an
#: implied "vetted by us", so an upload must not be able to claim it.
PSEUDO_CATEGORY_IDS = frozenset({"all", "official"})

#: Department code prefix -> category slug. Keys are upper-case and must match
#: on a whole-segment boundary (see ``resolve_group``). Order is irrelevant;
#: resolution always prefers the longest matching prefix.
#:
#: Deliberately absent:
#: * Anything outside ``BNEW RTE RP`` (``EEE``, ``TSM``, ``RCE``, ``RA``,
#:   ``BCSS``) — no portal category represents them, so their members get the
#:   full market and must pick a group by hand when they register something.
DEPT_GROUP_MAP: dict[str, str] = {
    "BNEW RTE RP PS": "product-system",
    "BNEW RTE RP IVC": "ivc",
    "BNEW RTE RP RD1": "radio-design-1",
    "BNEW RTE RP RD2": "radio-design-2",
    "BNEW RTE RP RD3": "radio-design-3",
    "BNEW RTE RP SWD": "radio-sw",
    "BNEW RTE RP QA": "qa",
    "BNEW RTE RP PO": "pmo",
    #
    # Radio Products Chengdu, by sub-team. There is deliberately **no**
    # ``BNEW RTE RP CD`` key: the site is not a function, so a site-wide key
    # could only send Chengdu's work to a tab that answers a different question
    # than the other nine (see the module docstring).
    #
    # Each mapping below is evidenced, not guessed, because a wrong group is the
    # one failure this module cannot detect: the asset lands in someone else's
    # filter, where its uploader will never look. Two kinds of evidence were
    # accepted, both measured against production:
    #
    # * The same sub-team code exists under a Beijing unit with the same human
    #   label, so the code names the discipline: ``CD FS Function System`` and
    #   ``PS FS Function System``; ``CD SW1/SW2/SW3 Software n`` and
    #   ``SWD SW1/SW2/SW3``; ``CD PA2 Power Amplifier 2`` and ``RD1 PA2``;
    #   ``CD IVC T1/T2 Team n`` against ``IVC``'s own ``T1``…``T6``.
    # * The team's own members already filed assets under that group by hand,
    #   before any of this was mapped — ``CD INT`` chose ``radio-design-1``
    #   twice and ``CD PS`` chose ``product-system``.
    #
    # ``CD PS`` is the one to read twice: the code matches Beijing's ``PS`` but
    # the labels do not — Chengdu's is *Performance* System, Beijing's is
    # *Product* System. It is mapped on its own members' filing choice, not on
    # the code collision, and it would be the first mapping to revisit if
    # somebody reports an asset in the wrong tab.
    "BNEW RTE RP CD IVC": "ivc",
    "BNEW RTE RP CD IVC2": "ivc",
    "BNEW RTE RP CD FS": "product-system",
    "BNEW RTE RP CD PS": "product-system",
    "BNEW RTE RP CD SW1": "radio-sw",
    "BNEW RTE RP CD SW2": "radio-sw",
    "BNEW RTE RP CD SW3": "radio-sw",
    "BNEW RTE RP CD INT": "radio-design-1",
    "BNEW RTE RP CD PA2": "radio-design-1",
    #
    # Left unmapped on purpose: ``CD ET`` (Enclosure&Thermal), ``CD FIC``
    # (Filter & IC), ``CD PPD`` (Power PCB Digital), ``CD TRX`` (Transceiver),
    # and Chengdu people with no sub-team code at all. 11 people, **0 assets**
    # between them, and no counterpart code under any Beijing unit — a mapping
    # would be a guess dressed as a fact.
    #
    # Unmapped is safe here in a way it was not before: it yields ``None``, so
    # the upload dialog pre-fills nothing and the person picks. What made the old
    # arrangement leak was ``cd`` being *present* and pre-filled — that is how
    # ``CD FS``'s first upload landed in ``cd`` three days after this table was
    # planned, from a sub-team that had no uploader when the plan was drawn up.
}

#: The group for an asset whose owner has a department that no prefix above
#: covers — today that is everyone outside ``BNEW RTE RP`` (``RA RD5`` is the
#: live case: 5 assets from 5 real employees). Only the automatic importers file
#: into it; see the module docstring for why the upload dialogs do not.
#:
#: It is a real, fileable category, unlike ``PSEUDO_CATEGORY_IDS``: a person in
#: an unmapped department has to be able to pick it, otherwise their only way
#: past the "选择分组" check is to claim a department that is not theirs.
FALLBACK_GROUP_ID = "others"

#: Label used when a group's category row has to be created (see
#: ``ensure_group_categories``). Only consulted for a slug that has no row yet —
#: an existing label is never overwritten, because this runs on every boot and a
#: migration must not rewrite a value somebody set in the database.
#:
#: Note there is **no rename endpoint**: ``POST /api/categories`` answers 409 for
#: an id that already exists, and nothing else writes ``RpAiCategory.label``. So
#: changing a label that is already in a database is a direct ``UPDATE`` today,
#: and editing this table alone only affects databases that lack the row.
GROUP_CATEGORY_LABELS: dict[str, str] = {
    "product-system": "Product System",
    "ivc": "IVC",
    "radio-design-1": "Radio Design 1",
    "radio-design-2": "Radio Design 2",
    "radio-design-3": "Radio Design 3",
    "radio-sw": "Radio SW",
    "qa": "QA",
    "pmo": "PMO",
    FALLBACK_GROUP_ID: "Others",
}

#: Slugs this module used to hand out and no longer does. Kept as a named list
#: because dropping a mapping does **not** remove the category row: nothing in
#: this module deletes rows, and ``ensure_group_categories`` only inserts. So a
#: database that ever ran the old code still has ``cd`` and ``ai-team`` rows, and
#: they have to be deleted deliberately, **after** their assets are refiled.
#:
#: Order matters and the two failure modes differ, which is why this is written
#: down rather than left to whoever runs the delete:
#:
#: * ``rp_ai_skill.category_id`` has a foreign key with ``ondelete=SET NULL`` —
#:   deleting the row silently un-files its skills.
#: * ``rp_ai_market_item.category_id`` has **no** foreign key — deleting the row
#:   leaves items pointing at a slug with nothing behind it. Those rows show up
#:   in no tab and are still counted in "All", which is precisely the
#:   "tabs do not add up" bug this repo has already fixed once.
RETIRED_GROUP_IDS: frozenset[str] = frozenset({"cd", "ai-team"})


def resolve_group(department: str | None) -> str | None:
    """Return the category slug for an LDAP department path, or ``None``.

    Matching is per segment, longest prefix first, so ``BNEW RTE RP PS AI ...``
    resolves to ``ai-team`` and ``BNEW RTE RP PS FS ...`` to
    ``product-system``. A department that no prefix covers returns ``None``.
    """
    if not department:
        return None
    # Whitespace in the directory is not always a single space.
    segments = department.upper().split()
    for end in range(len(segments), 0, -1):
        hit = DEPT_GROUP_MAP.get(" ".join(segments[:end]))
        if hit:
            return hit
    return None


def verified_group(db: Session, department: str | None) -> str | None:
    """``resolve_group`` plus a check that a category row actually backs it.

    The table above is code and the categories are data, so they can drift — a
    category gets renamed or dropped. Handing out a slug the market has no row
    for would filter every page to nothing, so an unbacked slug counts as "no
    group" and is logged.
    """
    slug = resolve_group(department)
    if not slug:
        return None
    if db.get(RpAiCategory, slug) is None:
        logger.warning("Department %r maps to unknown category %r", department, slug)
        return None
    return slug


def group_for_eid(db: Session, eid: str | None) -> str | None:
    """The verified group of a portal user, from the department stored at login."""
    if not eid:
        return None
    row = db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid))
    return verified_group(db, row.department if row else None)


def group_or_fallback(db: Session, department: str | None) -> str:
    """``verified_group`` but never ``None`` — an unmapped department gives ``others``.

    For the **automatic** importers only. They have no human to ask, and their
    old answer (no group at all) produced rows that were counted in a market's
    "All" tab and reachable from no department tab. ``others`` is the honest
    version of that: still "not one of our departments", but visible.

    Returns the fallback even if the ``others`` row is somehow missing, unlike
    ``verified_group``'s refusal to hand out an unbacked slug: the row is created
    on every boot by ``ensure_group_categories``, and a filter that shows nothing
    is a smaller failure than a row that shows up in no filter at all.
    """
    return verified_group(db, department) or FALLBACK_GROUP_ID


def ensure_group_categories(db: Session) -> list[str]:
    """Create a category row for every group slug that lacks one.

    Returns the slugs it created.

    Covers ``DEPT_GROUP_MAP``'s values **plus** ``FALLBACK_GROUP_ID``, which is
    not in that table because it is not a department prefix — it is where the
    importers put an owner no prefix matches. Leaving it out would make
    ``group_or_fallback`` hand out a slug with no row, i.e. a tab that does not
    exist, which is the bug it was written to fix.

    This exists because the two halves of a group live in different places: the
    slug is code (the table above) and the category is data (a table row), and
    ``verified_group`` deliberately refuses a slug with no row behind it. So
    adding a mapping without the row is a silent no-op — every member of that
    department keeps resolving to "no group", and the only symptom is a default
    that never fills in.

    Nothing else creates these rows. The seed script is for a fresh database and
    is not run on deploy; ``seed_market`` is deprecated. The container entrypoint
    runs ``app.migrate``, which is why this is called from there.

    Only ever inserts. An existing label is left alone because this runs on every
    boot and must not rewrite a value somebody set in the database. That also
    means a rename is **not** picked up here: there is no rename endpoint
    (``POST /api/categories`` answers 409 for an existing id), so renaming a
    group that already has a row is a direct ``UPDATE`` on that row, and updating
    the table above only helps databases that do not have the row yet.
    """
    created: list[str] = []
    for slug in sorted(set(DEPT_GROUP_MAP.values()) | {FALLBACK_GROUP_ID}):
        if db.get(RpAiCategory, slug) is not None:
            continue
        label = GROUP_CATEGORY_LABELS.get(slug)
        if not label:
            # A mapping was added without a label. Refuse to invent one: the
            # label is what people read on the tab, and a slug rendered as a tab
            # title ("radio-design-1") looks like a bug that nobody owns.
            logger.warning("No label for group %r; category row not created", slug)
            continue
        db.add(RpAiCategory(id=slug, label=label, count=0))
        created.append(slug)
    if created:
        db.commit()
    return created
