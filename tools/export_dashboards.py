#!/usr/bin/env python3
"""Export Grafana dashboards into git.

The UI is where dashboards are edited, which means the real dashboards live in
Grafana's own database — a SQLite file inside a container. That is fine until the
volume is lost, someone recreates the stack without one, or a person leaves and
nobody knows what the board looked like. This pulls them out as JSON so they can
be committed.

Output goes to ``grafana/backup/``, deliberately NOT to
``grafana/dashboards/`` (the provisioned path): a snapshot placed there would be
re-applied by the provisioner within seconds and start overwriting the UI it was
copied from. Restoring is a manual, deliberate act — import the JSON through
Dashboards -> New -> Import.

Usage (on a host that can reach Grafana):

    export GRAFANA_URL=http://localhost:9401
    export GRAFANA_TOKEN=<service account token>      # preferred
    # or: export GRAFANA_USER=admin GRAFANA_PASSWORD=...
    python3 tools/export_dashboards.py [--out DIR] [--dry-run]

Create the token in Grafana under Administration -> Service accounts, with the
Viewer role: exporting only reads. Nothing is written back to Grafana.

Standard library only, and no syntax newer than 3.6, so it runs with whatever
python3 the deploy host happens to have (this workspace's is 3.6).
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://localhost:9401"
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "grafana", "backup")

#: Keys that describe *this* Grafana's copy rather than the dashboard itself.
#: Dropped so the file is a clean, importable definition and so re-exporting an
#: unchanged dashboard produces an unchanged file (no version churn in git).
VOLATILE_KEYS = ("id", "version")


def _request(url: str, headers: dict) -> object:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _auth_headers() -> dict:
    """Bearer token if given, else basic auth, else fail loudly.

    Refusing to run unauthenticated is deliberate: an anonymous Grafana would
    silently export nothing and look like "no dashboards" rather than "no
    permission".
    """
    token = os.environ.get("GRAFANA_TOKEN", "").strip()
    if token:
        return {"Authorization": "Bearer " + token}
    user = os.environ.get("GRAFANA_USER", "").strip()
    password = os.environ.get("GRAFANA_PASSWORD", "")
    if user:
        raw = ("%s:%s" % (user, password)).encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
    sys.exit("Set GRAFANA_TOKEN (preferred) or GRAFANA_USER/GRAFANA_PASSWORD.")


def _slug(text: str) -> str:
    """A stable, filesystem-safe file name, so re-exports overwrite in place."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "dashboard"


def list_dashboards(base: str, headers: dict) -> list:
    """Every dashboard this token can see (folders and other types excluded)."""
    url = base.rstrip("/") + "/api/search?" + urllib.parse.urlencode(
        {"type": "dash-db", "limit": 5000}
    )
    return [item for item in _request(url, headers) if item.get("uid")]


def fetch_dashboard(base: str, headers: dict, uid: str) -> dict:
    url = base.rstrip("/") + "/api/dashboards/uid/" + urllib.parse.quote(uid)
    return _request(url, headers)


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=DEFAULT_OUT, help="output directory")
    parser.add_argument(
        "--dry-run", action="store_true", help="list what would be written, write nothing"
    )
    args = parser.parse_args(argv[1:])

    base = os.environ.get("GRAFANA_URL", DEFAULT_URL)
    headers = _auth_headers()

    try:
        items = list_dashboards(base, headers)
    except urllib.error.HTTPError as exc:
        return _fail("%s returned HTTP %s (%s)" % (base, exc.code, exc.reason))
    except urllib.error.URLError as exc:
        return _fail("cannot reach %s: %s" % (base, exc.reason))

    if not items:
        print("No dashboards found at %s (check the token's permissions)." % base)
        return 0

    if not args.dry_run:
        os.makedirs(args.out, exist_ok=True)

    written = 0
    for item in sorted(items, key=lambda i: i.get("title") or ""):
        uid = item["uid"]
        try:
            payload = fetch_dashboard(base, headers, uid)
        except urllib.error.HTTPError as exc:
            print("  ! %s: HTTP %s" % (uid, exc.code))
            continue

        dashboard = payload.get("dashboard") or {}
        for key in VOLATILE_KEYS:
            dashboard.pop(key, None)
        folder = (payload.get("meta") or {}).get("folderTitle") or ""

        name = "%s-%s.json" % (_slug(dashboard.get("title") or item.get("title")), uid)
        path = os.path.join(args.out, name)
        print("  %s %-46s %s" % ("would write" if args.dry_run else "wrote", name,
                                 "(folder: %s)" % folder if folder else ""))
        if not args.dry_run:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(dashboard, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
        written += 1

    print("\n%d dashboard(s) from %s -> %s" % (written, base, args.out))
    if not args.dry_run:
        print("Review and commit them; this directory is a backup, not provisioning.")
    return 0


def _fail(message: str) -> int:
    print("error: " + message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
