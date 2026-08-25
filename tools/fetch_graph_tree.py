#!/usr/bin/env python
"""Walk the organisation from Microsoft Graph and cache it for the report.

Run this to refresh ``graph_tree_cache.json``. ``build_report.py`` reads that file and
prefers it over the LDAP walk, because Graph is the source behind the org chart in
Teams and the reader compares the report against Teams.

    python fetch_graph_tree.py                  # token from GRAPH_TOKEN or ./graph_token
    python fetch_graph_tree.py --token-file /path/to/token
    python fetch_graph_tree.py --token-file t \\
        --push https://portal.internal --portal-token <your portal JWT>

Getting a token, in order of preference:

1. **App registration** (unattended, survives past an hour). One Entra app with the
   *application* permission ``User.Read.All`` and admin consent. ``Directory.Read.All``
   is not needed and is harder to get approved. Then set ``GRAPH_TENANT_ID``,
   ``GRAPH_CLIENT_ID`` and ``GRAPH_CLIENT_SECRET`` and this script fetches its own
   token — no extra packages, it is one HTTPS POST.
2. **Graph Explorer**, for a one-off. Sign in at
   https://developer.microsoft.com/graph/graph-explorer, copy the access token from
   the Access token tab into a file, and pass ``--token-file``. Expires in about an
   hour. Write it to a file rather than pasting it anywhere it will be kept.

Why the tree is walked level by level with ``directReports`` rather than asked for
directly: ``transitiveReports`` gives a count but refuses to enumerate
("Enumerating transitive reports is not supported"), and the report needs the people,
not the number. ``$batch`` keeps this to about fifty requests instead of a thousand.

Read-only. It issues GETs against ``/users`` and never writes to the directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
#: Default output. The service reads this path from USAGE_REPORT_ORG_SNAPSHOT, so
#: the two are kept in step by pointing this at the same place rather than writing
#: next to the script and leaving somebody to copy it across.
CACHE = Path(
    os.environ.get(
        "USAGE_REPORT_ORG_SNAPSHOT", "/var/lib/rp-ai-portal/graph_tree_cache.json"
    )
)
# The walk itself lives in the service, so the portal's Sync button and this
# script cannot end up with two definitions of "department".
from app.services.usage_report.graph_walk import GRAPH, walk  # noqa: E402
#: Yong Shen A, Head of PEU Radio Pacific — the root the org chart hangs from.
#: Whose reporting tree to walk — the report covers this person and everyone under
#: them. Overridable because the post-holder changes and the id does not say who it
#: is: a successor would otherwise have to find this line to understand why the
#: report still describes the previous organisation.
ROOT_ID = os.environ.get(
    "GRAPH_ROOT_ID", "7055e248-9d31-44a6-a44e-e06415abfff8"
)


def token_from_client_credentials() -> str | None:
    """A token of our own, if the app registration details are configured."""
    tenant = os.environ.get("GRAPH_TENANT_ID")
    client = os.environ.get("GRAPH_CLIENT_ID")
    secret = os.environ.get("GRAPH_CLIENT_SECRET")
    if not (tenant and client and secret):
        return None
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "client_id": client,
            "client_secret": secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def resolve_token(token_file: str | None) -> str:
    if token_file:
        return Path(token_file).read_text().strip()
    from_app = token_from_client_credentials()
    if from_app:
        return from_app
    env = os.environ.get("GRAPH_TOKEN")
    if env:
        return env.strip()
    default = HERE / "graph_token"
    if default.exists():
        return default.read_text().strip()
    sys.exit(
        "No token. Set GRAPH_TENANT_ID/GRAPH_CLIENT_ID/GRAPH_CLIENT_SECRET, "
        "or GRAPH_TOKEN, or pass --token-file. See the module docstring."
    )


def push(base_url: str, portal_token: str, people: dict) -> None:
    """Upload the walk to the portal, which keeps it in its own database.

    Pushed rather than fetched by the service: this walk needs a Graph token, takes
    minutes, and the service holds neither. Storing it server-side means a deploy
    does not need a mounted file and a redeploy does not lose it.
    """
    url = base_url.rstrip("/") + "/api/department-report/org-snapshot"
    response = requests.put(
        url,
        json=people,
        headers={"Authorization": "Bearer " + portal_token},
        timeout=300,
    )
    if response.status_code != 200:
        raise SystemExit(
            f"  upload failed: HTTP {response.status_code} {response.text[:400]}"
        )
    got = response.json()
    print(f"\n  uploaded to {url}")
    print(f"    portal stored {got['people']} people, {got['placed']} placed")
    print(f"    departments: {' '.join(got['departments'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", help="File holding a Graph access token")
    ap.add_argument("--out", default=str(CACHE))
    ap.add_argument(
        "--push",
        metavar="PORTAL_URL",
        help="Also upload to the portal, e.g. https://rp-ai-portal.internal",
    )
    ap.add_argument(
        "--portal-token",
        help="Your portal session token (the JWT the browser sends). Required "
        "with --push; must belong to an EID on the Dashboard list.",
    )
    args = ap.parse_args()
    if args.push and not args.portal_token:
        ap.error("--push needs --portal-token")

    people = walk(resolve_token(args.token_file), ROOT_ID)
    Path(args.out).write_text(json.dumps(people, ensure_ascii=False))

    from collections import Counter

    counts = Counter(p["branch"] for p in people.values())
    disabled = sum(1 for p in people.values() if not p.get("accountEnabled"))
    print(f"\n  wrote {args.out}: {len(people)} people, {disabled} disabled")
    for code, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {code:<6} {n}")

    if args.push:
        push(args.push, args.portal_token, people)


if __name__ == "__main__":
    main()
