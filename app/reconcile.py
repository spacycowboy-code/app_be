"""Reconcile the audit projection with the review MRs.

Two triggers, one convergence function:

    python -m app.reconcile                 one sweep, then exit (cron, or by hand)
    python -m app.reconcile --watch         sweep every RECONCILE_INTERVAL seconds
    POST /api/audit/reconcile               webhook, for latency

The periodic sweep is what makes it correct: webhooks are lossy, so a system that
only reacts to events drifts silently. The webhook only shortens the delay.
"""
import argparse
import logging
import os
import time

from app.database import SessionLocal
from app.services.skill_audit.reconcile import reconcile_all

log = logging.getLogger("skill_audit.reconcile")


def sweep() -> dict:
    with SessionLocal() as db:
        return reconcile_all(db)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="keep sweeping")
    ap.add_argument("--interval", type=int,
                    default=int(os.environ.get("RECONCILE_INTERVAL", "300")),
                    help="seconds between sweeps in --watch mode")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if not args.watch:
        result = sweep()
        print(f"examined={result['examined']} changed={result['changed']}")
        return

    log.info("reconciling every %ds", args.interval)
    while True:
        try:
            result = sweep()
            if result["changed"]:
                log.info("changed: %s", result["changed"])
        except Exception:  # noqa: BLE001 - a sweep failure must not end the loop
            log.exception("sweep failed; retrying next interval")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
