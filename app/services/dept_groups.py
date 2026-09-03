"""Map an LDAP department path to a portal group (a category slug).

LDAP hands us ``department`` as a space-separated path of org codes followed by
the last code's human label::

    BNEW RTE RP IVC TE Test Environment
    ^^^^ ^^^ ^^ ^^^ ^^ ^^^^^^^^^^^^^^^^
    codes, coarse to fine        label of the last code

The portal's groups sit at the fourth level (``IVC``, ``PS``, ``QA``, ``RD1``…),
one level *above* the team a person actually belongs to, so the two cannot be
compared directly — hence this table.

Why prefixes and not whole strings: the full value identifies a team, and there
are 50+ of them in a directory that reorganises. The codes at the front are the
stable part, and a prefix match keeps working when a new team appears under a
known unit. It also survives the ``String(64)`` column: truncation eats the
trailing label, never the leading codes.

Why longest-match-first: it lets one team be pulled out of its parent's group
without touching the parent (``RP PS AI`` -> ``ai-team`` while the rest of
``RP PS`` stays ``product-system``).

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
    # A team carved out of its parent unit: listed first for the reader, but it
    # wins because it is longer, not because it is earlier.
    "BNEW RTE RP PS AI": "ai-team",
    "BNEW RTE RP PS": "product-system",
    "BNEW RTE RP IVC": "ivc",
    "BNEW RTE RP RD1": "radio-design-1",
    "BNEW RTE RP RD2": "radio-design-2",
    "BNEW RTE RP RD3": "radio-design-3",
    "BNEW RTE RP SWD": "radio-sw",
    "BNEW RTE RP QA": "qa",
    "BNEW RTE RP PO": "pmo",
    # Radio Products Chengdu — the largest single group (68 of the 287 people
    # who have signed in). It was left out while no portal category represented
    # it; the effect was that every Chengdu upload landed in whatever the
    # dialog happened to default to.
    #
    # It sits LAST in the reader's eye but matters most for one reason: it also
    # swallows ``RP CD IVC`` and ``RP CD PS``, which are **Chengdu's own** IVC
    # and PS teams — not the ``IVC`` / ``product-system`` these slugs mean.
    # Longest-prefix matching is what keeps them apart: ``BNEW RTE RP CD IVC``
    # matches this key (4 segments) and never ``BNEW RTE RP IVC``, because the
    # codes sit at a different depth. Filing a ``RP CD IVC`` person under
    # ``ivc`` would put Chengdu's work under Beijing's name, and nothing in the
    # UI would look wrong.
    "BNEW RTE RP CD": "cd",
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
    "ai-team": "AI Team",
    "product-system": "Product System",
    "ivc": "IVC",
    "radio-design-1": "Radio Design 1",
    "radio-design-2": "Radio Design 2",
    "radio-design-3": "Radio Design 3",
    "radio-sw": "Radio SW",
    "qa": "Q&A",
    "pmo": "PMO",
    "cd": "Chengdu",
    FALLBACK_GROUP_ID: "Others",
}


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
