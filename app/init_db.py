"""Create all database tables and verify the connection.

Usage:
    python -m app.init_db
"""
from sqlalchemy import text

from app.database import Base, engine

# Import models so they register on Base.metadata.
import app.models  # noqa: F401


def main() -> None:
    # Verify connectivity first.
    with engine.connect() as conn:
        version = conn.execute(text("SELECT VERSION()")).scalar()
        print(f"Connected to MySQL: {version}")

    Base.metadata.create_all(bind=engine)
    print("Tables ensured:")
    for table in Base.metadata.sorted_tables:
        print(f"  - {table.name}")


if __name__ == "__main__":
    main()
