"""Walk the Microsoft Graph reporting tree: everyone under one person.

This is where the department head counts in the usage report come from. Graph is
used rather than the portal's own ``department`` column or an LDAP walk because it
is the graph the Teams org chart draws, and a report whose per-department numbers
cannot be reconciled against the chart the reader has open gets discarded whatever
it says. Measured: LDAP's manager chain returns 961 people against Graph's 945 and
differs by up to seven in a single department.

Reached over ``$batch``, twenty managers per request. Asking for transitive
reports would be one call, but Graph refuses to enumerate them
(``Enumerating transitive reports is not supported``) — only their count.

Called two ways, deliberately sharing one implementation: from the portal, with a
token a reader pastes in, and from ``tools/fetch_graph_tree.py`` for unattended
refreshes. A second copy of this would be a second definition of "department".
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
SELECT = (
    "id,displayName,onPremisesSamAccountName,department,jobTitle,"
    "accountEnabled,mail,employeeType,userType"
)
#: Depth limit. The real tree is six deep; this only stops a cycle in the
#: directory from walking forever.
MAX_DEPTH = 12


class GraphWalkFailed(RuntimeError):
    """The walk could not be completed, so there is no new snapshot to store."""


class GraphTokenRejected(GraphWalkFailed):
    """Graph would not accept the token — expired, or lacking the directory scope."""


def _batched(
    session: requests.Session, ids: list[str], timeout: int
) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for start in range(0, len(ids), 20):
        chunk = [
            {
                "id": pid,
                "method": "GET",
                "url": f"/users/{pid}/directReports?$select={SELECT}&$top=999",
            }
            for pid in ids[start : start + 20]
        ]
        response = None
        for attempt in range(4):
            response = session.post(
                GRAPH + "/$batch",
                json={"requests": chunk},
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            if response.status_code == 200:
                break
            if response.status_code == 401:
                raise GraphTokenRejected("Graph rejected the token")
            # Graph throttles per app; backing off is the documented remedy.
            time.sleep(2 * (attempt + 1))
        else:
            raise GraphWalkFailed(
                f"$batch failed after 4 attempts: HTTP "
                f"{response.status_code if response is not None else '?'}"
            )
        for item in response.json().get("responses", []):
            if item.get("status") == 200:
                out[item["id"]] = item["body"].get("value", [])
            else:
                # Retried singly rather than silently dropping a manager's team: a
                # missing team is a department that quietly reports too few people.
                single = session.get(
                    GRAPH + f"/users/{item['id']}/directReports"
                    f"?$select={SELECT}&$top=999",
                    timeout=timeout,
                )
                out[item["id"]] = (
                    single.json().get("value", []) if single.status_code == 200 else []
                )
    return out


def _code4(department: object) -> str | None:
    """The fourth code in the department path — ``BNEW RTE RP >CD< …``."""
    tokens = str(department or "").split()
    return tokens[3] if len(tokens) > 3 else None


def walk(token: str, root_id: str, timeout: int = 120) -> dict:
    """Everyone under ``root_id``, keyed by Graph object id.

    Each record carries the branch it was reached through, which is what makes a
    department here the same set of people as the department in Teams.
    """
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": "Bearer " + token,
            # Required by Graph for the advanced query options used here.
            "ConsistencyLevel": "eventual",
        }
    )

    response = session.get(GRAPH + f"/users/{root_id}?$select={SELECT}", timeout=timeout)
    if response.status_code == 401:
        raise GraphTokenRejected("Graph rejected the token")
    if response.status_code == 404:
        raise GraphWalkFailed(f"no such user in Graph: {root_id}")
    if response.status_code != 200:
        raise GraphWalkFailed(
            f"could not read the root user: HTTP {response.status_code}"
        )
    root = response.json()

    people = {root["id"]: dict(root, manager_id=None, depth=0, branch=None)}
    frontier = [root["id"]]
    depth = 0
    while frontier and depth < MAX_DEPTH:
        depth += 1
        found = _batched(session, frontier, timeout)
        nxt = []
        for pid, kids in found.items():
            for kid in kids:
                if kid["id"] in people:
                    continue
                people[kid["id"]] = dict(kid, manager_id=pid, depth=depth, branch=None)
                nxt.append(kid["id"])
        logger.info("Graph walk level %d: +%d (total %d)", depth, len(nxt), len(people))
        frontier = nxt

    # Each of the root's direct reports heads a branch; everyone under them inherits
    # its code. This is the step that makes these departments Teams' departments.
    children: dict[str | None, list[str]] = {}
    for pid, record in people.items():
        children.setdefault(record["manager_id"], []).append(pid)
    for top in children.get(root["id"], []):
        code = _code4(people[top].get("department")) or "?"
        stack = [top]
        while stack:
            current = stack.pop()
            people[current]["branch"] = code
            stack += children.get(current, [])
    people[root["id"]]["branch"] = _code4(people[root["id"]].get("department")) or "?"
    return people
