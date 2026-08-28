"""Turn the one department string we have into a two-level org grouping.

LDAP was checked first for something structured — a departmentNumber, a division,
an organizational OU chain — and has none of it. The DN is an Active Directory
container path (OU=CA,OU=User,OU=P001,OU=ID,OU=Data), not the business org, and
the only organisational attribute is ``department``, the same free-text string
already stored in rp_ai_portal. So the hierarchy has to be read out of that
string, and this module is where that reading is written down rather than being
spread over the queries.

The string is a path of short org codes followed by a human-readable label:

    BNEW RTE RP IVC TE Test Environment
    ^^^^^^^^^^^^ ^^^ ^^ ^^^^^^^^^^^^^^^^
    org prefix   L4  L5  label of the leaf

Two observations from the 55 distinct values actually present decide the design:

* Positional slicing of the *whole* string is unsafe. Depth ranges from 6 to 9
  tokens, and five values sit outside BNEW RTE RP entirely (BNEW RTE TSM ...,
  BNEW RCE CCI ..., BNEW RTE EEE ...). Only the leading run of code tokens is
  reliable, and only its first few entries.
* The leaf label alone is ambiguous. "IVC T1 Team 1" exists under both RP and
  RP CD — two different teams with identical names. Any key built from the label
  would silently merge them.

Hence: the small department is keyed by its full code path, and the big
department by the fourth code. Keying the small department by code path also
merges the two spellings of RD2 DIG ("Digital" and "Digital HW Design"), which a
key built from the raw string would report as two departments with one person
each.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: An org code: short, no lowercase, and containing at least one letter.
#:
#: The letter requirement and the exclusion of ``&`` both come from real values.
#: ``BNEW RTE RP QA QCM2 QA&CM 2`` ends in a human label, "QA&CM 2", whose two
#: tokens are both uppercase — so a pattern that only tested for absence of
#: lowercase swallowed them as codes and produced the key ``RP/QA/QCM2/QA&CM/2``,
#: splitting one unit off from itself. A bare number is a label suffix
#: ("Enclosure 1", "QA&CM 2"), never a code, and an ampersand only ever appears
#: inside a name ("Thermal&IC", "Int & Ver & Cert").
_CODE = re.compile(r"^(?=[A-Z0-9]*[A-Z])[A-Z0-9]{1,5}$")

#: EIDs that are not a person and that LDAP cannot rule out, because it does not
#: know them at all. Functional accounts are *not* listed here — the directory
#: already distinguishes those by employeeType, and a hand-maintained list would
#: go stale the moment somebody creates another one. What remains is test rows
#: from portal development and display strings that reached the user table.
#:
#: Listed rather than pattern-matched so the report can show exactly what was left
#: out. A silent filter on a report for management is a way to be wrong without
#: anyone being able to see it.
NON_PERSON: dict[str, str] = {
    "ADMIN": "break-glass admin account",
    "ADMINMM": "break-glass admin account (local sign-ins during development)",
    "RP-AI-BOT": "service account that performs the official skill import",
    "AI TEAM": "not an EID; a display string that reached the user table",
    "ANTHROPIC": "not an EID; a display string that reached the user table",
    "ERICSSON": "not an EID; a display string that reached the user table",
    "EAB": "generic company account, no department in LDAP",
    # Test rows from portal development. Kept explicit rather than matched by
    # shape, so a real EID can never be dropped by a clever regex.
    "DFADFA": "test row",
    "E": "test row",
    "EACDS": "test row",
    "EBANCAI": "test row",
    "ECEDFF": "test row",
    "EZHMIO": "test row (mistyped EID)",
    "EZZZZZZZ": "test row",
    "HELLO": "test row",
}

#: The directory's own marker for a real employee. Everything else with
#: objectCategory=person under these departments is a functional account: a
#: JIRA integration, an AWS access ID, a build account, or somebody's second
#: admin ID. Under BNEW RTE RP that is 75 accounts against 943 employees.
WORKFORCE = "Workforce"

#: Shown in the report, so it is written in the language the report is written in.
UNKNOWN = "（查不到部门）"


@dataclass(frozen=True)
class Dept:
    """Where one person sits, at both levels the report groups by."""

    small_key: str      # full code path, e.g. BNEW/RTE/RP/IVC/TE
    small_label: str    # the department string as stored, for display
    big_key: str        # fourth code, e.g. IVC
    big_label: str      # with its parent for context, e.g. RP / IVC
    org_prefix: str     # first three codes, e.g. BNEW/RTE/RP


def code_run(department: str) -> list[str]:
    """The leading run of org codes. Stops at the first human-readable word.

    Public because the market cards need the same reading of the same string
    (see ``app/services/card_group.py``). Two readers of one free-text field
    must not each carry their own idea of where the codes stop — the ``QA&CM 2``
    and ``Enclosure 1`` cases above were found once and are worth paying for
    once.
    """
    out: list[str] = []
    for token in department.split():
        if _CODE.match(token):
            out.append(token)
        else:
            break
    return out


def parse(department: str | None) -> Dept:
    """Split one department string into its small and big grouping keys.

    Values with fewer than four codes cannot name a fourth-level unit, so they
    are grouped under their own deepest code rather than being forced into a
    level they do not have. "Executive Management Ericsson" has no codes at all
    and lands in UNKNOWN — correctly, since it says nothing about which unit the
    person works in.
    """
    if not department or not department.strip():
        return Dept(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN)

    department = " ".join(department.split())
    codes = code_run(department)
    if not codes:
        return Dept(UNKNOWN, department, UNKNOWN, UNKNOWN, UNKNOWN)

    small_key = "/".join(codes)
    org_prefix = "/".join(codes[:3])
    if len(codes) >= 4:
        big_code = codes[3]
        # Parent included because the code alone repeats across orgs: PS is a
        # unit under RP and also part of BNEW RTE RU PS.
        big_label = f"{codes[2]} / {big_code}"
        big_key = "/".join(codes[:4])
    else:
        big_code = codes[-1]
        big_label = " / ".join(codes)
        big_key = "/".join(codes)
    return Dept(small_key, department, big_key, big_label, org_prefix)


def resolve(
    eid: str,
    db_department: str | None,
    ldap_department: str | None,
    employee_type: str | None,
    known_to_ldap: bool,
) -> tuple[bool, Dept, str]:
    """Decide whether an EID counts as a person, and where they sit.

    Three ways to not be a person, in order of authority:

    * the directory says so — anything it knows but does not mark as Workforce is
      a functional account (the portal's own LDAP bind account, ``ECNRPAI``, is in
      the user table this way, and would otherwise count as an employee of the
      department it was filed under);
    * the hand-kept list, for EIDs the directory has never heard of;
    * nothing else. An EID the directory knows *and* marks Workforce is a person,
      whatever its name looks like — one real employee here has a non-standard
      account name, so any rule based on the shape of the EID would drop them.

    The database's department wins over LDAP's when both exist: it is what the
    portal itself acted on, so a figure derived from it can be reconciled against
    the product. LDAP only fills the gaps.
    """
    eid = eid.strip().upper()
    if known_to_ldap and (employee_type or "").strip() != WORKFORCE:
        return False, parse(None), "LDAP 中非 Workforce，是功能账号"
    if eid in NON_PERSON:
        return False, parse(None), NON_PERSON[eid]
    department = (db_department or "").strip() or (ldap_department or "").strip()
    return True, parse(department), "database" if (db_department or "").strip() else "LDAP"
