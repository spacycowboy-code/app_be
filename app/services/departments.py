"""The nine big departments, and which market/skill rows belong to each.

Attribution is by ``category_id`` — the group the uploader picked in the upload
dialog, which defaults to their own department (see ``dept_groups.py``). The
alternative was to attribute by the uploader's LDAP department, which is what the
department *report* does; it was measured against production and rejected,
because the two answers differ sharply: every item filed under ``radio-sw`` was
uploaded by someone whose directory department is Chengdu, so a report-style page
for Radio SW would have been empty while the market showed four items under that
very name. A page that disagrees with the market page it links to is worse than
one that follows the market's own filing.

The consequence to keep in mind: this is self-declared. Somebody can file a
skill under a group they do not belong to, and this page will believe them — the
same way the market page already does.

``CD`` has no category row. It is listed anyway because it is one of the nine
real departments and leaving it out of the sidebar would be its own kind of wrong
answer; its page renders honestly empty until a ``cd`` category exists. Adding
that row is a market-wide decision (it would appear as a new filter tab on the
Agent Market, the Skill Market and the upload dialog), not something this feature
gets to do on the side.
"""
from __future__ import annotations

from dataclasses import dataclass


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
    Department("qa", "QA", "QA"),
    Department("pmo", "PMO", "PMO"),
    Department("cd", "CD", "Radio Products Chengdu"),
)

#: Slug of the page holding everything the nine do not claim.
UNASSIGNED_SLUG = "unassigned"

UNASSIGNED = Department(UNASSIGNED_SLUG, "—", "Unassigned")

_BY_SLUG = {d.slug: d for d in DEPARTMENTS}

#: The category ids the nine departments own, for the "everything else" query.
DEPARTMENT_CATEGORY_IDS = frozenset(d.slug for d in DEPARTMENTS)


def department_by_slug(slug: str) -> Department | None:
    """The department for a URL segment, ``UNASSIGNED``, or ``None``."""
    if slug == UNASSIGNED_SLUG:
        return UNASSIGNED
    return _BY_SLUG.get(slug)
