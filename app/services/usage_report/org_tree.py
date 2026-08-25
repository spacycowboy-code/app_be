"""The organisation as Microsoft Graph reports it, which is what Teams displays.

Why this exists alongside the LDAP walk in ``roster.py``: the reader compares this
report against the org chart in Teams, and Teams is a view over Entra ID's
``manager``/``directReports`` graph. Reading the same graph removes a whole class of
argument about whose number is right.

Measured, not assumed. Both sources were pulled and compared:

* Filtered the same way — ``employeeType`` of Workforce, account enabled, grouped by
  the ``department`` attribute — Graph and on-premises LDAP agree exactly, 945 people
  and the same count in every unit. The directories hold the same data.
* Teams' figures come from neither of those groupings. They are the transitive
  reporting tree under each of the head's direct reports, counted without regard to
  whether the account is still enabled. ``transitiveReports/$count`` on the IVC head
  returns 124, plus the head is 125, which is the number on the reader's screen.

So the two totals that looked equal were different populations that happened to have
the same size: the reporting tree carries sixteen people whose accounts are disabled,
and grouping by ``department`` instead carries fourteen people who have no ``manager``
attribute and whom no walk can reach. That is why the totals agreed while individual
units did not.

This module converts the Graph tree into the shape ``roster.org_tree`` returns, so the
placement, stale-code correction and label logic in ``build_report`` stay unchanged and
keep whatever testing they have had.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _code4(department: object) -> str:
    tokens = str(department or "").split()
    return tokens[3] if len(tokens) > 3 else ""


def _sub_path(department: object) -> str:
    """Code path down to the sub-unit, empty when the department has no sub-unit.

    The fifth token has to be a real code. Accepting any fifth token reads
    "CD Radio Products Chengdu" as the sub-unit "Radio", which makes a department's
    own level look like a populated team.
    """
    tokens = str(department or "").split()
    if len(tokens) <= 4:
        return ""
    fifth = tokens[4]
    if fifth.upper() != fifth or not any(c.isalpha() for c in fifth):
        return ""
    return "/".join(tokens[:5])


def load(cache_path: Path) -> dict:
    """Read the cached Graph walk and return it shaped like ``roster.org_tree``.

    Keyed by signum, because that is what the portal stores against every download
    and login; the Graph object id is of no use for joining.
    """
    path = Path(cache_path)
    raw = json.loads(path.read_text())
    # Recorded here rather than stat()-ed at render time: the reader needs to know how
    # old the org structure is, and the renderer should not have to know where the
    # snapshot lives to tell them.
    as_of = datetime.fromtimestamp(path.stat().st_mtime)

    by_id = raw
    people: dict[str, dict] = {}
    id_to_signum: dict[str, str] = {}
    for oid, rec in by_id.items():
        signum = str(rec.get("onPremisesSamAccountName") or "").upper()
        if not signum:
            continue
        id_to_signum[oid] = signum

    for oid, rec in by_id.items():
        signum = id_to_signum.get(oid)
        if not signum:
            continue
        mgr = by_id.get(str(rec.get("manager_id") or ""), {})
        people[signum] = {
            "branch": rec.get("branch"),
            "department": rec.get("department"),
            "corrected": rec.get("department"),
            "manager_department": mgr.get("department"),
            "employeeType": rec.get("employeeType"),
            # Kept truthfully. The population is defined by membership of the
            # reporting tree, the way Teams defines it, so this is reported rather
            # than used as a filter — but a disabled account cannot sign in, and a
            # reader working out a percentage needs to know how many there are.
            "enabled": bool(rec.get("accountEnabled")),
            "title": rec.get("jobTitle"),
            "name": rec.get("displayName"),
            "depth": rec.get("depth"),
        }

    # Pass 1: a department whose fourth code disagrees with the branch the tree walk
    # arrived by is a stale code — HWD3 is the former name of RD3. The manager's
    # department names the unit that still exists.
    for signum, rec in people.items():
        if _code4(rec.get("department")) != str(rec.get("branch") or ""):
            if rec.get("manager_department"):
                rec["corrected"] = rec["manager_department"]
                rec["stale"] = True

    # Pass 2: a sub-unit of one whose only member's team or manager sits in another
    # populated sub-unit is a stale sub-unit code, not a team. Deliberately narrow:
    # "follow the manager" on its own is wrong because managers routinely have
    # reports spread over several sub-units.
    def reports_unit(department: object) -> tuple[str, str, int, int]:
        tally: dict[str, int] = {}
        full: dict[str, str] = {}
        for other in people.values():
            if other.get("manager_department") != department:
                continue
            key = _sub_path(other.get("corrected"))
            if not key or key == _sub_path(department):
                continue
            tally[key] = tally.get(key, 0) + 1
            full.setdefault(key, str(other.get("corrected") or ""))
        if not tally:
            return "", "", 0, 0
        best = max(tally, key=lambda k: tally[k])
        return best, full[best], tally[best], sum(tally.values())

    merged: list[dict] = []

    def note(signum: str, rec: dict, target: str, why: str) -> None:
        merged.append(
            {
                "signum": signum,
                "from": rec.get("corrected"),
                "to": target,
                "why": why,
                "title": rec.get("title"),
            }
        )
        rec["corrected"] = target
        rec["merged_subunit"] = True

    # Rule 1, applied per sub-unit rather than per person: if nobody in a sub-unit is
    # managed from inside it, and every member reports into the same other sub-unit,
    # the code names no team. Two people whose department reads "QA QCM2 QA&CM 2" both
    # report to the manager of In-Field Quality, so they are part of her group; giving
    # them a row of their own put a two-person "unit" in the list of teams nobody has
    # reached, next to real teams of eighteen.
    #
    # A genuine team fails this test by construction — its own manager sits in it — so
    # the rule cannot dissolve one. Checked against the whole organisation before
    # being applied: of fifty-three sub-units it matches exactly this one.
    members: dict[str, list[str]] = {}
    for signum, rec in people.items():
        key = _sub_path(rec.get("corrected"))
        if key:
            members.setdefault(key, []).append(signum)

    for unit, group in sorted(members.items()):
        targets = {_sub_path(people[m].get("manager_department")) for m in group}
        if len(targets) != 1:
            continue
        target_unit = next(iter(targets))
        if not target_unit or target_unit == unit:
            continue
        if len(members.get(target_unit, [])) < 2:
            continue
        full = next(
            (
                str(people[m].get("corrected") or "")
                for m in members[target_unit]
                if _sub_path(people[m].get("corrected")) == target_unit
            ),
            "",
        )
        if not full:
            continue
        for m in group:
            note(m, people[m], full, "全组都汇报给这个单位的经理")

    # Rule 2: a lone member whose own reports sit elsewhere. This catches the opposite
    # shape — a manager carrying a stale code while the team below them carries the
    # current one — which rule 1 cannot see, because that manager's own manager is a
    # level further up and often at the department's own level.
    sizes = {k: len(v) for k, v in members.items()}
    for signum, rec in list(people.items()):
        if rec.get("merged_subunit"):
            continue
        own = _sub_path(rec.get("corrected"))
        if not own or sizes.get(own, 0) != 1:
            continue
        unit, full_dept, hits, total = reports_unit(rec.get("corrected"))
        if unit and hits >= 2 and hits * 2 > total and sizes.get(unit, 0) >= 2:
            note(signum, rec, full_dept, f"{hits} 名下属在此")
            continue
        mgr_sub = _sub_path(rec.get("manager_department"))
        if mgr_sub and mgr_sub != own and sizes.get(mgr_sub, 0) >= 2:
            note(signum, rec, str(rec.get("manager_department") or ""), "经理在此")

    root_signum = ""
    for signum, rec in people.items():
        if rec.get("depth") == 0:
            root_signum = signum
            break

    return {
        "as_of": as_of.isoformat(timespec="minutes"),
        "source": "graph",
        "root": root_signum,
        "found": bool(people),
        "head": {
            "signum": root_signum,
            "department": people.get(root_signum, {}).get("department"),
        },
        "merged_subunits": merged,
        "disabled": sum(1 for r in people.values() if not r.get("enabled")),
        "people": people,
    }
