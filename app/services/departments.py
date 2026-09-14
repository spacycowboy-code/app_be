"""The nine big departments, and which market/skill rows belong to each.

Attribution is by the uploader's **LDAP organisation** — the ``department`` string
the directory gave us at their last sign-in, read down to its fourth org code
(``BNEW RTE RP IVC TE …`` -> ``IVC`` -> the ``ivc`` page). It is deliberately
**not** ``category_id``, the group picked in the upload dialog.

This reverses an earlier decision, and the reasoning that was replaced is worth
keeping because it was correct about the facts:

    Attributing by the uploader's LDAP department was measured against
    production and rejected, because the two answers differ sharply: every item
    filed under ``radio-sw`` was uploaded by someone whose directory department
    is Chengdu, so a report-style page for Radio SW would have been empty while
    the market showed four items under that very name. A page that disagrees
    with the market page it links to is worse than one that follows the market's
    own filing.

Both halves of that still happen. What changed is that the disagreement is now
the **intended** design rather than a symptom: the two markets answer "what
function is this asset for?" and these pages answer "which organisation produced
it?", so Chengdu's verification tooling is meant to appear under the ``IVC``
market tab *and* under Chengdu's department page. Those are different questions
about one asset, and one number cannot serve both.

The old objection about linking no longer applies either: this page links to
``/skills/:id``, ``/skills`` and ``/market`` with no category filter, so it never
promises that a market tab will show the same set.

What this costs, stated plainly because it is the price of the change: an asset
whose owner has no usable directory department places nowhere. That is not a
rounding error — it is 24 of 115 rows in production, mostly IVC skills whose
owner rows were created by a sync that never wrote ``department``. They land on
the ``Unassigned`` page, which is why that page is not optional.

The self-declared failure mode of the old basis is gone (nobody can put their
asset on another department's page by picking a group), and a new one takes its
place: the directory is now load-bearing, so a wrong ``department`` in LDAP moves
assets between pages and cannot be corrected from inside the portal.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.usage_report.departments import code_run


@dataclass(frozen=True)
class Department:
    #: URL segment, and the ``category_id`` rows are matched on.
    slug: str
    #: The short code people actually say out loud. Used in the sidebar.
    code: str
    #: The long form, for the page heading.
    label: str


#: Order is the order they appear in the sidebar.
DEPARTMENTS: tuple[Department, ...] = (
    Department("product-system", "PS", "Product System"),
    Department("ivc", "IVC", "IVC"),
    Department("radio-design-1", "RD1", "Radio Design 1"),
    Department("radio-design-2", "RD2", "Radio Design 2"),
    Department("radio-design-3", "RD3", "Radio Design 3"),
    Department("radio-sw", "RSW", "Radio SW"),
    # Label and code are both ``QA`` (Quality Assurance). The label position read
    # "Q&A" for a while, and that spells "questions and answers" — a different
    # thing from the department, so it was changed back. Either way the report's
    # LDAP segment names (``dept_groups.DEPT_GROUP_MAP``, ``BNEW RTE RP QA``) are
    # a separate namespace this does not touch.
    Department("qa", "QA", "QA"),
    Department("pmo", "PMO", "PMO"),
    Department("cd", "CD", "Radio Products Chengdu"),
)

#: Slug of the page holding everything the nine do not claim.
UNASSIGNED_SLUG = "unassigned"

UNASSIGNED = Department(UNASSIGNED_SLUG, "—", "Unassigned")

_BY_SLUG = {d.slug: d for d in DEPARTMENTS}

#: Fourth org code -> department slug. This is the **organisational** reading and
#: is intentionally a different table from ``dept_groups.DEPT_GROUP_MAP``, which
#: reads the same directory string for a *function*. Chengdu is the case that
#: makes the difference visible: here ``CD`` is one department, while over there
#: ``CD IVC`` is verification work and ``CD SW1`` is software.
#:
#: Keyed on the bare code rather than a path prefix because the code at the
#: fourth position is already unique inside ``BNEW RTE RP`` — and the org prefix
#: is checked separately, so a same-named unit under a different top-level org
#: cannot collide with these.
BRANCH_TO_SLUG: dict[str, str] = {
    "PS": "product-system",
    "IVC": "ivc",
    "RD1": "radio-design-1",
    "RD2": "radio-design-2",
    "RD3": "radio-design-3",
    "SWD": "radio-sw",
    "QA": "qa",
    "PO": "pmo",
    "CD": "cd",
}

#: The org codes above the departments. A department string that does not start
#: with these is outside RP entirely (``BNEW RTE RA RD5`` is the live case) and
#: has no page, by design — inventing one would put five people's assets under a
#: heading no reader could place.
RP_ORG_PREFIX: tuple[str, ...] = ("BNEW", "RTE", "RP")

#: The category ids the nine departments own.
#:
#: No longer used to attribute assets — that is now the directory's job — but
#: still needed by callers that reason about the *category* namespace, where a
#: department slug and a category slug happen to be spelled the same.
DEPARTMENT_CATEGORY_IDS = frozenset(d.slug for d in DEPARTMENTS)


def department_for_directory_value(department: str | None) -> Department | None:
    """The department page for one LDAP ``department`` string, or ``None``.

    ``None`` means "this string does not name an RP department" and is returned
    for all four ways that happens, which are deliberately not distinguished
    here because they all render the same page: an empty value, a value whose
    leading codes are not ``BNEW RTE RP``, a value with fewer than four codes
    (it cannot name a fourth-level unit), and a fourth code with no page.

    Reads the codes with ``usage_report.departments.code_run`` rather than
    splitting the string here. That function is public for exactly this reason
    and its docstring says so: a free-text directory field with three separate
    readers drifts, and it already carries the ``QA&CM 2`` / ``Enclosure 1``
    lessons. It also declines to read ``Radio`` in
    ``BNEW RTE RP CD Radio Products Chengdu`` as a code, because it requires an
    all-upper-case token — a naive ``.upper().split()`` invents a ``RADIO``
    sub-team there, which is a mistake this codebase has made twice.
    """
    if not department:
        return None
    codes = code_run(department)
    if len(codes) < 4 or tuple(codes[:3]) != RP_ORG_PREFIX:
        return None
    return _BY_SLUG.get(BRANCH_TO_SLUG.get(codes[3], ""))


def department_by_slug(slug: str) -> Department | None:
    """The department for a URL segment, ``UNASSIGNED``, or ``None``."""
    if slug == UNASSIGNED_SLUG:
        return UNASSIGNED
    return _BY_SLUG.get(slug)
