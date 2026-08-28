"""The three-part group shown on a skill / tool / agent card.

    地址 (BJ | CD) · 大部门 · 小部门

It describes **the person who uploaded the item**, not the market category the
uploader filed it under. The two are different axes that happen to share the
word "组": a Chengdu engineer can file an agent under the Radio SW category, and
this label still says CD. The category answers "who is it for", this answers
"who made it". Keeping them apart is why this module does not touch
``category_id``.

Where the data comes from
-------------------------
``rp_ai_portal.department``, the raw LDAP path written at every sign-in. The
directory has no location attribute at all — checked for ``officeLocation``,
``city``, ``l`` and ``usageLocation``, none is fetched and none is stored — so
the site has to be read out of the same path as the two department levels:

    BNEW RTE RP IVC TE Test Environment      -> BJ · IVC · TE
    BNEW RTE RP CD SW1 Software 1            -> CD · SW1
    BNEW RTE RP CD IVC T1 Team 1             -> CD · IVC · T1
    ^^^^^^^^^^^^ ^^^ ^^^ ^^^^^^^^^^^^^^^^^
    org prefix   L4  L5  label of the last code

So ``CD`` is not a separate dimension: it is the fourth code, sitting on the
same axis as ``IVC`` and ``SWD``. That is what makes the rule asymmetric —
Chengdu spends L4 on naming the site, and its departments therefore start one
level deeper. Chengdu is also flatter: most of its units (``PPD``, ``TRX``,
``SW1``…) hang directly off ``CD`` and have no sub-unit, so their 小部门 is
legitimately empty rather than missing.

Why derive instead of storing a 53-row table
--------------------------------------------
The rule below was checked against the full list of 53 sub-units that exist in
the organisation today and reproduces 51 of them exactly. A table would too,
but a table also answers "nothing" for the 54th team, and the way that failure
shows up is a card that silently loses its group line. Same argument as
``dept_groups.DEPT_GROUP_MAP`` makes for prefix matching: the codes are the
stable part, and a rule keeps working when a new team appears under a known
unit.

Why nothing is stored on the item row
-------------------------------------
People change departments. A group copied onto the skill row at upload time
would keep displaying where its author used to sit, and nobody would ever
notice it had gone stale. This is computed per request from the department the
portal read at the author's last sign-in, for the same reason
``core/group.service.ts`` deliberately keeps the portal group out of the JWT.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import RpAiPortal
from app.services.usage_report.departments import code_run

#: The organisation this label is defined for. A department outside it (``BNEW
#: RTE TSM …``, ``BNEW RCE CCI …``, and the handful of EEE paths) gets no group
#: rather than a guessed one: the BJ/CD split is a statement about Radio
#: Products' two sites, and neither answer would be true of an EEE employee.
_ORG_PREFIX = ("BNEW", "RTE", "RP")

#: The fourth code that names a site rather than a department.
_CHENGDU = "CD"

#: Sub-units whose site is not what the path implies.
#:
#: Both are Chengdu-organisation units that the owner of this feature listed as
#: Beijing-located, which is a fact no reading of the path can produce — the
#: path only knows the org tree. They are the *only* two exceptions among the 53
#: sub-units, and they are written verbatim as supplied, including the unusual
#: shape (``CD`` kept as the 大部门 with the sub-unit demoted to 小部门), because
#: guessing at an intent behind someone else's org data is how a card ends up
#: asserting the wrong office.
#:
#: If these two turn out to be a data-entry slip for ``CD PS`` / ``CD INT``,
#: deleting the two rows below is the whole fix — the rule already handles them.
_EXCEPTIONS: dict[tuple[str, ...], tuple[str, str | None, str | None]] = {
    ("CD", "PS"): ("BJ", "CD", "PS"),
    ("CD", "INT"): ("BJ", "CD", "INT"),
}


@dataclass(frozen=True)
class CardGroup:
    """Where the author of an item sits, as shown on its card."""

    site: str                 # BJ | CD
    big_dept: str | None      # IVC, SWD, PS… (Chengdu: PPD, TRX, IVC…)
    small_dept: str | None    # TE, T1, SW3… empty for a flat Chengdu unit
    department: str           # the raw LDAP path, for the card's tooltip

    @property
    def label(self) -> str:
        """The parts that exist, joined — "BJ IVC TE", "CD PPD", or just "CD"."""
        return " ".join(p for p in (self.site, self.big_dept, self.small_dept) if p)


def group_for_department(department: str | None) -> CardGroup | None:
    """Read the three-part group out of one LDAP department path.

    Returns ``None`` when the path cannot support the label: absent, outside
    Radio Products, or with too few codes to name a fourth-level unit. A card
    then shows no group line at all, which is the honest answer — an item whose
    author we cannot place must not claim an office.
    """
    if not department or not department.strip():
        return None
    codes = code_run(" ".join(department.split()))
    if tuple(c.upper() for c in codes[:3]) != _ORG_PREFIX:
        return None

    rest = [c.upper() for c in codes[3:]]
    if not rest:
        return None

    override = _EXCEPTIONS.get(tuple(rest[:2]))
    if override:
        site, big, small = override
        return CardGroup(site, big, small, department)

    if rest[0] == _CHENGDU:
        # Chengdu spends the fourth code on the site, so its departments start
        # at the fifth. ``BNEW RTE RP CD Radio Products Chengdu`` — the site's
        # own level — legitimately has neither part.
        site, rest = _CHENGDU, rest[1:]
    else:
        site = "BJ"

    big = rest[0] if rest else None
    small = rest[1] if len(rest) > 1 else None
    return CardGroup(site, big, small, department)


def groups_for_eids(db: Session, eids: set[str]) -> dict[str, CardGroup]:
    """Resolve many authors at once. One query, whatever the page size.

    Batched rather than per-item because both market lists render every matching
    row: a per-card lookup would put one query per card behind a page that
    already answers in one.

    EIDs are matched case-insensitively via upper-cased keys, the way the rest
    of the portal treats them — a download recorded against ``ezhgmio`` and a
    skill uploaded by ``EZHGMIO`` are the same person.
    """
    wanted = {e.strip().upper() for e in eids if e and e.strip()}
    if not wanted:
        return {}
    rows = db.execute(
        select(RpAiPortal.eid, RpAiPortal.department).where(
            RpAiPortal.eid.in_(wanted)
        )
    ).all()
    out: dict[str, CardGroup] = {}
    for eid, department in rows:
        group = group_for_department(department)
        if group:
            out[str(eid).strip().upper()] = group
    return out
