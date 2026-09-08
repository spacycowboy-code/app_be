"""Create ``rp_ai_portal`` rows for people who have never signed in.

Why this exists
---------------
Two things the portal shows are keyed on a person having a row in
``rp_ai_portal``: the card's org-group badge (``services/card_group``) and the
department placement of their contributions in the usage report
(``build_people`` in ``services/usage_report/render``). Both read
``rp_ai_portal.department``.

That is fine as long as everybody whose work is in the portal put it there
themselves — the row is created on their first sign-in. It stops being fine when
work is *registered on someone's behalf* (a bulk import of an existing tool
catalogue, say): the market row names them as the creator, but there is no
account row, so the badge is blank and the report files their work under
"无法归属".

Inserting the row fixes both. What it must not do is make the report claim they
signed in — ``ever_signed_in`` counts rows in this table. Hence
``origin='import'``, which that one metric filters out. See
``app/models/user.py::PORTAL_ROW_ORIGINS``.

The department is read from the directory, never passed in: a hand-typed
department is a number in a report that nobody can trace back to a source.

Usage (from the backend repo root, so .env is picked up)::

    .venv/bin/python tools/register_directory_users.py EENYNAM EFANRUI ...
    .venv/bin/python tools/register_directory_users.py --apply EENYNAM ...

Dry run is the default and prints exactly what it would write. Re-running is
safe: an EID that already has a row is left untouched and reported, whatever its
origin — in particular this never rewrites a real sign-in row to 'import'.
"""
from __future__ import annotations

import argparse
import ssl
import sys

from ldap3 import ALL, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models.user import RpAiPortal

#: Column width in the model. Truncating here rather than letting MySQL do it
#: keeps the printed plan honest about what lands in the row.
DEPARTMENT_MAX = 64
DISPLAY_NAME_MAX = 128


def lookup(eids: list[str]) -> dict[str, dict[str, str]]:
    """EID -> {name, department} straight from the directory.

    Search-only, with the service account: no user password is involved. An EID
    the directory does not know is simply absent from the result.
    """
    settings = get_settings()
    server = Server(
        settings.ldap_url,
        use_ssl=settings.ldap_url.lower().startswith("ldaps"),
        tls=Tls(validate=ssl.CERT_NONE) if not settings.ldap_verify_tls else None,
        get_info=ALL,
    )
    conn = Connection(
        server,
        user=settings.ldap_bind_user,
        password=settings.ldap_bind_password,
        auto_bind=True,
        receive_timeout=settings.ldap_timeout,
    )
    found: dict[str, dict[str, str]] = {}
    try:
        for eid in eids:
            # sAMAccountName, not the eid attribute: that is what a signum is,
            # and it is what ldap_auth searches on at login.
            ok = conn.search(
                settings.ldap_base_dn,
                f"(sAMAccountName={eid})",
                search_scope=SUBTREE,
                attributes=[
                    settings.ldap_display_attr,
                    settings.ldap_department_attr,
                    "sAMAccountName",
                ],
            )
            if not ok or not conn.entries:
                continue
            entry = conn.entries[0]
            name = str(entry[settings.ldap_display_attr]) if settings.ldap_display_attr in entry else ""
            dept = str(entry[settings.ldap_department_attr]) if settings.ldap_department_attr in entry else ""
            found[eid] = {"name": name.strip(), "department": dept.strip()}
    finally:
        conn.unbind()
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("eids", nargs="+", help="Signums / EIDs to register")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the rows (default is a dry run)",
    )
    args = parser.parse_args()

    eids = sorted({e.strip().upper() for e in args.eids if e.strip()})
    if not eids:
        print("No EIDs given.")
        return 2

    try:
        directory = lookup(eids)
    except LDAPException as exc:
        print(f"LDAP lookup failed: {exc}")
        return 1

    db = SessionLocal()
    try:
        existing = {
            row.eid.strip().upper(): row
            for row in db.scalars(select(RpAiPortal).where(RpAiPortal.eid.in_(eids)))
        }

        planned: list[RpAiPortal] = []
        for eid in eids:
            row = existing.get(eid)
            if row is not None:
                print(f"  = {eid:<9} already has a row (origin={row.origin}) — left alone")
                continue
            rec = directory.get(eid)
            if rec is None:
                print(f"  ! {eid:<9} not found in the directory — skipped")
                continue
            if not rec["department"]:
                # A row with no department buys nothing: the badge needs it and
                # so does the report's placement. Saying so beats writing a row
                # that looks registered and still shows blank.
                print(
                    f"  ! {eid:<9} {rec['name']!r} has no department in the "
                    "directory — skipped, a row would not place them anywhere"
                )
                continue
            planned.append(
                RpAiPortal(
                    eid=eid,
                    display_name=rec["name"][:DISPLAY_NAME_MAX] or None,
                    department=rec["department"][:DEPARTMENT_MAX],
                    origin="import",
                )
            )
            print(
                f"  + {eid:<9} {rec['name']:<22} {rec['department'][:DEPARTMENT_MAX]}"
            )

        if not planned:
            print("\nNothing to write.")
            return 0
        if not args.apply:
            print(f"\nDry run: {len(planned)} row(s) would be created. Re-run with --apply.")
            return 0

        db.add_all(planned)
        db.commit()
        print(f"\nCreated {len(planned)} row(s) with origin='import'.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
