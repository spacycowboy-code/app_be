"""One-off: import the radioai tool catalogue export as market agents.

Not a router and not a reusable feature. It exists as a file so the decisions it
encodes are reviewable and the run is repeatable, rather than living in a shell
history. Dry run is the default.

Why not POST /api/market: that endpoint hard-codes ``creator`` to the caller
(``creator = acting_eid(request, ...)``), and the whole point here is that each
row is credited to the person who actually built the agent. It also has no field
for ``is_trending``.

Decisions baked in, all settled with the owner of the data:

* every row is an ``agent`` reached over the web -> ``service_type='cloud'``,
  ``service_url`` = the CSV's ``url``
* ``author`` is the person's directory display name, ``creator`` their signum
* three signums are outside BNEW RTE RP (one in RA, two external contractors);
  their rows are credited to their manager, whose signum is in TRACY, while
  ``author`` still names the real builder
* ``category_id`` comes from the *creator's* directory department through the
  portal's own ``resolve_group``
* ``created_at`` is left to the database, i.e. the import date. The CSV's own
  timestamps are unusable: 13 of 18 were exported through Excel as ``mm:ss.0``
  with the date component gone. Accepted knowingly — see the note printed at the
  end of a run, which is there so the number lands in the report with its
  caveat rather than silently.
* two ``url`` values are not URLs and are rewritten (see ``URL_FIXES``)
"""
from __future__ import annotations

import argparse
import csv
import sys

from sqlalchemy import select

from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.routers.market import _slugify
from app.services.dept_groups import resolve_group

#: Directory answers, captured 2026-08-31. Kept as data rather than looked up at
#: run time so the same input always produces the same rows: this is a one-off
#: whose result has to be explainable months from now, and a directory that
#: reorganises would otherwise change what a re-run writes.
DIRECTORY = {
    "EENYNAM": ("Mengchi Yang", "BNEW RTE RP SWD SW3 Software 3"),
    "EERIADA": ("Adam Eriksson", "BNEW RTE RA RD5 SWD Software Design D"),
    "EFANRUI": ("Ruijun Fan", "BNEW RTE RP SWD Software Design"),
    "EHECANF": ("Chenyu Fan A", "BNEW RTE RP SWD SW2 Software 2"),
    "EHNEYXA": ("Hexuan Yang", "BNEW RTE RP SWD SW2 Software 2"),
    "EHSHAZA": ("Shaolei Zhang", "BNEW RTE RP SWD SW3 Software 3"),
    "EJNOQSJ": ("Lu Cheng", "BNEW RTE RP SWD SW2 Software 2"),
    "ENAWNFA": ("Iris Wang B", "BNEW RTE RP SWD SW2 Software 2"),
    "EOSOUGN": ("Mark Song", "BNEW RTE RP SWD SW2 Software 2"),
    "EXAJIWU": ("Wendy Wu A", "BNEW RTE RP SWD SW3 Software 3"),
    "EXINIJL": ("Jinhang Li", "BNEW RTE RP PS AI AI & Innovation"),
    "EYZXUHO": ("Joey Zhou", "BNEW RTE RP SWD SW2 Software 2"),
    "ZOLXUYI": ("You Li (EXT)", ""),
    "ZQIURIC": ("Richard Qiu (EXT)", ""),
    "ECUIDEN": ("Tracy Deng", "BNEW RTE RP SWD SW2 Software 2"),
}

#: Signums with no place in BNEW RTE RP: their work is credited to their manager,
#: because a creator the report cannot place contributes to nobody. ``author``
#: still names the person who built it, so the card does not misattribute the
#: work — the same split the market already uses for agents registered by a
#: colleague on someone's behalf.
TRACY = "ECUIDEN"
CREDIT_TO_MANAGER = {"EERIADA": TRACY, "ZOLXUYI": TRACY, "ZQIURIC": TRACY}

#: CSV rows whose ``url`` is not a URL. Both were pasted by hand into a field
#: that expects an address.
URL_FIXES = {
    # "20139529: NOREF: DP slot info decode | https://..." — the Gerrit change
    # title was pasted in front of the link. Keep the link.
    "44": "https://gerrit-sigma.gic.ericsson.se/c/radio/ai-assets/+/20139529",
    # A whole `git clone ... && curl ... commit-msg` shell command. There is no
    # page in it to open: ssh:// is a clone address. Point at the project's own
    # page on Gerrit instead, which is the closest thing to "where this lives".
    "50": "https://gerrit-sigma.gic.ericsson.se/admin/repos/zolxuyi/youAgent",
}

#: is_recommended in the export.
TRENDING_CSV_IDS = {"31", "38", "42"}

#: The one row that is not an agent: a knowledge-base file download. Excluded by
#: the owner of the data.
SKIP_CSV_IDS = {"40"}

SOURCE = "radioai"


def unique_id(db, name: str, taken: set[str]) -> str:
    """``_slugify`` plus the same numeric suffix ``create_item`` would apply."""
    base = _slugify(name)
    candidate = base
    n = 2
    while candidate in taken or db.get(RpAiMarketItem, candidate) is not None:
        candidate = f"{base}-{n}"
        n += 1
    taken.add(candidate)
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description="Import the radioai export")
    parser.add_argument("csv_path")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    with open(args.csv_path, encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh)]

    db = SessionLocal()
    try:
        taken: set[str] = set()
        planned: list[RpAiMarketItem] = []
        for r in rows:
            csv_id = (r["id"] or "").strip()
            if csv_id in SKIP_CSV_IDS:
                print(f"  - skip id={csv_id} {r['name']!r} (not an agent)")
                continue
            if (r["category"] or "").strip() != "Agent":
                print(f"  - skip id={csv_id} {r['name']!r} (category={r['category']!r})")
                continue

            author_eid = (r["author_eid"] or "").strip().upper()
            author_name, _ = DIRECTORY[author_eid]
            creator = CREDIT_TO_MANAGER.get(author_eid, author_eid)
            _, creator_dept = DIRECTORY[creator]
            category_id = resolve_group(creator_dept)

            url = URL_FIXES.get(csv_id, (r["url"] or "").strip())
            name = (r["name"] or "").strip()
            item_id = unique_id(db, name, taken)

            planned.append(
                RpAiMarketItem(
                    id=item_id,
                    name=name,
                    description=(r["description"] or "").strip() or None,
                    item_type="agent",
                    service_type="cloud",
                    service_url=url,
                    repo_url=None,
                    embeddable=False,
                    source=SOURCE,
                    creator=creator,
                    author=author_name,
                    version=(r["version"] or "").strip() or None,
                    tags=[t.strip() for t in (r["tags"] or "").split("|") if t.strip()],
                    category_id=category_id,
                    is_trending=csv_id in TRENDING_CSV_IDS,
                    status="approved",
                )
            )
            flag = " [trending]" if csv_id in TRENDING_CSV_IDS else ""
            fixed = " [url rewritten]" if csv_id in URL_FIXES else ""
            print(
                f"  + id={csv_id:<3} {item_id:<32} {author_name:<18} "
                f"creator={creator:<8} {category_id}{flag}{fixed}"
            )

        existing = set(
            db.scalars(
                select(RpAiMarketItem.name).where(
                    RpAiMarketItem.name.in_([i.name for i in planned])
                )
            ).all()
        )
        if existing:
            # Not fatal by itself — the market does not dedupe on name — but for a
            # one-off import a repeat run is far more likely than two agents that
            # genuinely share a name, so stop and let a person look.
            print(f"\nABORT: these names are already in the market: {sorted(existing)}")
            return 1

        print(f"\n{len(planned)} row(s) planned.")
        if not args.apply:
            print("Dry run. Re-run with --apply.")
            return 0

        db.add_all(planned)
        db.commit()
        print(f"Created {len(planned)} market items with source={SOURCE!r}.")
        print(
            "NOTE: created_at is the import date. 13 of these had no usable date "
            "in the export, so this batch counts as new assets in the month it "
            "was imported, not the month each agent was built."
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
