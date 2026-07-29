"""SQLAlchemy engine, session factory and declarative base."""
from collections.abc import Generator
from datetime import timedelta
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# pool_pre_ping recycles dead connections (important for long-lived MySQL pools).
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.debug,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@lru_cache(maxsize=1)
def db_utc_offset() -> timedelta:
    """Offset between the DB server's local clock and UTC.

    Timestamps are written by the DB (``server_default=func.now()``) in the DB
    server's local time zone and stored as naive DATETIME values. This offset
    lets us label them correctly when serializing to the frontend as UTC.
    Cached for the process lifetime (recomputed on restart).
    """
    try:
        with engine.connect() as conn:
            seconds = conn.execute(
                text("SELECT TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(), NOW())")
            ).scalar()
        return timedelta(seconds=int(seconds or 0))
    except Exception:
        # Fall back to treating stored times as UTC if detection fails.
        return timedelta(0)
