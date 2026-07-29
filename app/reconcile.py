"""Entry point for the audit projection reconciler: ``python -m app.reconcile``."""
import logging

from app.database import SessionLocal
from app.services.skill_audit.reconcile import reconcile_all

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    with SessionLocal() as db:
        result = reconcile_all(db)
    print(f"examined={result['examined']} changed={result['changed']}")
