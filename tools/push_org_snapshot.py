"""Upload an org snapshot the portal can serve the department report from.

For when the walk has already been done and the file is sitting on disk. Fetching
it again needs a Graph token that expires in an hour; this needs only a portal
login.

    python tools/push_org_snapshot.py graph_tree_cache.json \\
        --portal https://portal.internal --portal-token <your portal JWT>

The token is the one the browser already holds: sign in to the portal, open the
developer tools, and copy it out of the Authorization header on any /api request.
It must belong to an EID on the Dashboard list.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import requests


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot", help="The graph_tree_cache.json to upload")
    ap.add_argument("--portal", required=True, help="Portal base URL")
    ap.add_argument("--portal-token", required=True, help="Your portal session JWT")
    args = ap.parse_args()

    path = Path(args.snapshot)
    if not path.is_file():
        print(f"  no such file: {path}")
        return 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Checked here as well as server-side: a mistyped path that happens to hold
    # other JSON would otherwise come back as a 422 the caller has to decode.
    if not isinstance(payload, dict) or not payload:
        print("  that file is not a Graph walk (expected a JSON object of users)")
        return 1

    # The file's own timestamp, not now: the report tells its reader how old the org
    # structure is, and uploading a week-old walk must not make it look fresh.
    captured = datetime.fromtimestamp(path.stat().st_mtime)
    url = args.portal.rstrip("/") + "/api/department-report/org-snapshot"
    response = requests.put(
        url,
        json=payload,
        params={"capturedAt": captured.isoformat(timespec="seconds")},
        headers={"Authorization": "Bearer " + args.portal_token},
        timeout=300,
    )
    if response.status_code == 401:
        print("  the portal did not accept that token (expired? copy a fresh one)")
        return 1
    if response.status_code == 403:
        print("  that EID is not on the Dashboard list, so it may not upload this")
        return 1
    if response.status_code != 200:
        print(f"  upload failed: HTTP {response.status_code} {response.text[:400]}")
        return 1

    got = response.json()
    print(f"  uploaded {len(payload)} records to {url}")
    print(f"    walked at {got['capturedAt']} (from the file's timestamp)")
    print(f"    portal stored {got['people']} people, {got['placed']} placed")
    print(f"    departments: {' '.join(got['departments'])}")
    print("  the report will use this from now on; no file needed on the server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
