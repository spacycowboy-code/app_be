"""The organisation the report counts by, as machine-readable JSON.

The report is HTML because it is read by people. This is the same organisation for
everything else: pasting the department tree into another tool, diffing this
month's structure against last month's, or seeding a spreadsheet.

**Built from the same call the report's own head counts come from**
(:func:`render.org_attribution`, with the same arguments :func:`render.build_html`
passes it), not from the raw Graph payload. That matters more than it looks:
between the payload and the page sit the stale-code correction (HWD3 is the former
name of RD3) and the sub-unit merging, and a second reading of the same payload
would produce a file that disagreed with the numbers printed on the page — by a
handful of people, in a way nobody could explain and no exception would report.

So the rule is: if the page says IVC has 30 people, this file says 30 too, or the
tests fail.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.engine import Connection

from app.services.reporting_windows import reporting_tz
from app.services.usage_report.render import load_org_tree, org_attribution


def build_structure(conn: Connection, org_snapshot: object = None) -> dict:
    """Shape the stored org snapshot into departments, teams and members.

    Raises :class:`OrgSnapshotMissing` when no structure has been pushed — the
    same failure, on the same terms, as the report itself. Guessing a structure
    here would produce a file that looks authoritative and is invented.
    """
    tree = load_org_tree(conn, org_snapshot)
    # Identical arguments to build_html's own call. Both are spelled out rather
    # than defaulted, because both defaults are the *other* answer: skip=set()
    # keeps everybody, and require_enabled=False keeps the disabled accounts that
    # the org chart in Teams also shows.
    placement, big_head, small_head, labels, _skipped = org_attribution(
        tree, set(), require_enabled=False
    )

    people = tree.get("people", {})
    # signum -> team key, grouped two levels down.
    teams: dict[str, dict[str, list[dict]]] = {}
    dept_names: dict[str, str] = {}
    for signum, dept in placement.items():
        record = people.get(signum, {})
        # branch_display was applied when the Dept was built, so big_label is
        # already the name the page shows (PO reads "PMO").
        dept_names.setdefault(dept.big_key, dept.big_label)
        teams.setdefault(dept.big_key, {}).setdefault(dept.small_key, []).append(
            {
                "eid": signum,
                "name": record.get("name") or signum,
                "title": record.get("title"),
                "department": record.get("corrected") or record.get("department"),
                # Reported, not filtered: membership of the reporting tree is what
                # defines the population (that is how the org chart defines it),
                # but a disabled account cannot sign in and a reader working out a
                # percentage has to be able to see how many there are.
                "enabled": bool(record.get("enabled")),
                # The two corrections the report applied, carried so a reader of
                # this file can tell a placement that came straight from the
                # directory from one this service reasoned about.
                "staleDepartmentCode": bool(record.get("stale")),
                "mergedSubunit": bool(record.get("merged_subunit")),
            }
        )

    departments = []
    for code, by_team in teams.items():
        departments.append(
            {
                "code": code,
                "name": dept_names.get(code, code),
                # The page's number, from the page's source. Equal to the length of
                # the member lists by construction — pinned by a test, because if
                # the two ever come apart the file would still look fine.
                "people": big_head.get(code, 0),
                "teams": [
                    {
                        "code": team_key,
                        "name": labels.get(team_key, (team_key, ""))[0],
                        "people": small_head.get(team_key, 0),
                        "members": sorted(members, key=lambda m: m["eid"]),
                    }
                    # Largest first, matching how the report orders its tables; the
                    # code breaks ties so the file is byte-stable between exports
                    # and a diff shows real changes only.
                    for team_key, members in sorted(
                        by_team.items(),
                        key=lambda kv: (-small_head.get(kv[0], 0), kv[0]),
                    )
                ],
            }
        )
    departments.sort(key=lambda d: (-d["people"], d["code"]))

    not_workforce = sum(
        1 for r in people.values() if r.get("employeeType") != "Workforce"
    )
    root = tree.get("root") or ""
    return {
        "generatedAt": datetime.now(reporting_tz()).isoformat(timespec="minutes"),
        # When the directory was read — the age that actually matters here. The
        # file is a snapshot of a snapshot.
        "capturedAt": tree.get("as_of"),
        "source": tree.get("source"),
        "head": {
            "eid": root,
            "name": people.get(root, {}).get("name") or root or None,
            "department": people.get(root, {}).get("department"),
        },
        "totals": {
            # What is in this file.
            "people": sum(d["people"] for d in departments),
            "departments": len(departments),
            "teams": sum(len(d["teams"]) for d in departments),
            "disabled": sum(
                1 for m in _members(departments) if not m["enabled"]
            ),
            # What was in the snapshot, and the difference, stated. Without these
            # two an export that dropped a hundred people would read as a smaller
            # organisation rather than as a lossy export.
            "inSnapshot": len(people),
            "notWorkforce": not_workforce,
        },
        "departments": departments,
    }


def _members(departments: list[dict]):
    for dept in departments:
        for team in dept["teams"]:
            yield from team["members"]
