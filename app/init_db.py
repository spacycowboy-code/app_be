"""Create all database tables and verify the connection.

Ensures every table is created with the ``utf8mb4`` charset so 4-byte
characters (e.g. emoji used in skill example output) are stored correctly.

Usage:
    python -m app.init_db            # create missing tables
    python -m app.init_db --recreate # drop rp_ai_* tables then recreate
"""
import sys

from sqlalchemy import text

from app.database import Base, engine

# Import models so they register on Base.metadata.
import app.models  # noqa: F401

# Make CREATE TABLE emit utf8mb4 for every table.
for _table in Base.metadata.tables.values():
    _table.kwargs["mysql_charset"] = "utf8mb4"
    _table.kwargs["mysql_collate"] = "utf8mb4_unicode_ci"


def main() -> None:
    recreate = "--recreate" in sys.argv

    with engine.connect() as conn:
        version = conn.execute(text("SELECT VERSION()")).scalar()
        print(f"Connected to MySQL: {version}")

    if recreate:
        Base.metadata.drop_all(bind=engine)
        print("Dropped existing tables.")

    Base.metadata.create_all(bind=engine)
    print("Tables ensured (utf8mb4):")
    for table in Base.metadata.sorted_tables:
        print(f"  - {table.name}")


if __name__ == "__main__":
    main()
