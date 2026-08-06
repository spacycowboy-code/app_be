"""Lightweight, idempotent schema migration.

``create_all`` only creates missing tables; it does not add new columns to
existing ones. This helper adds the columns introduced for skill storage and
creates any new tables (e.g. rp_ai_skill_version) with the utf8mb4 charset.

Usage:
    python -m app.migrate
"""
from sqlalchemy import text

from app.database import Base, engine

import app.models  # noqa: F401  (register models on Base.metadata)

# Ensure new tables are created as utf8mb4.
for _t in Base.metadata.tables.values():
    _t.kwargs["mysql_charset"] = "utf8mb4"
    _t.kwargs["mysql_collate"] = "utf8mb4_unicode_ci"

# (table, column, DDL type) tuples to add if missing.
COLUMNS = [
    ("rp_ai_skill", "storage_path", "VARCHAR(512) NULL"),
    ("rp_ai_skill", "repo_commit", "VARCHAR(64) NULL"),
    ("rp_ai_market_item", "last_activity", "VARCHAR(40) NULL"),
    ("rp_ai_market_item", "creator", "VARCHAR(64) NULL"),
    ("rp_ai_skill_download", "eid", "VARCHAR(64) NULL"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).scalar()
    return bool(row)


def main() -> None:
    # 1) Create any missing tables (rp_ai_skill_version).
    Base.metadata.create_all(bind=engine)

    # 2) Add missing columns to existing tables.
    with engine.begin() as conn:
        for table, column, ddl in COLUMNS:
            if _column_exists(conn, table, column):
                print(f"  = {table}.{column} already present")
            else:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                print(f"  + added {table}.{column}")
    print("Migration complete.")


if __name__ == "__main__":
    main()
