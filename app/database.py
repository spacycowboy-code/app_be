"""SQLAlchemy engine, session factory and declarative base."""
import logging
import time
from collections.abc import Generator
from datetime import timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# pool_pre_ping recycles dead connections (important for long-lived MySQL pools).
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.debug,
)

# 建立一条新连接失败后重试一次，中间隔 CONNECT_RETRY_DELAY 秒。
#
# 为什么需要它：``pool_pre_ping`` 只治「池里那条连接已经死了」——它把死连接丢掉，然后照样要
# 新建一条，而**新建**这一步失败时没有任何人重试，异常直接冒到路由、变成用户看见的 500。
# 2026-09-17 03:02:55–03:03:03 就是这样：容器有 8 秒解析不出库的主机名，一个人打开技能页的
# 四个请求（``/api/skills``、``/api/skills/{id}``、``/api/skills/{id}/comments``、
# ``/api/users``）全部 500，而库本身一直是好的。
#
# 为什么只重试一次：这类抖动（DNS 解析、握手超时）是秒级的，一次退避就能跨过去；库真的宕了
# 的话再多重试也只是把每个请求拖得更久、把连接池占满，那种情况应当快速失败让上面看见。
#
# 代价（明确记下来）：一次请求最坏要等两轮建连超时。pymysql 默认 ``connect_timeout`` 是 10 秒，
# 所以最坏约 20 秒 + 0.5 秒退避，而不是原来的 10 秒。要收紧就给 ``connect_args`` 加显式
# ``connect_timeout``，但那会影响所有建连、不只是重试路径，故本次不动。
CONNECT_RETRY_DELAY = 0.5


@event.listens_for(engine, "do_connect")
def _connect_with_one_retry(
    dialect: Any, conn_rec: Any, cargs: tuple, cparams: dict
) -> Any:
    """Build a new DBAPI connection, retrying once on failure.

    Returning a non-``None`` value tells SQLAlchemy to use this connection
    instead of making its own; ``dialect.connect(*cargs, **cparams)`` is exactly
    what it would have called, so the first attempt is byte-for-byte the default
    behaviour and only the retry is new.
    """
    try:
        return dialect.connect(*cargs, **cparams)
    except Exception as first:
        logger.warning(
            "DB connect failed (%s: %s); retrying once in %.1fs",
            type(first).__name__,
            first,
            CONNECT_RETRY_DELAY,
        )
        time.sleep(CONNECT_RETRY_DELAY)
        # 第二次仍失败就照原样抛出：SQLAlchemy 会把它包成 OperationalError，
        # 与没有这个钩子时的行为一致，不改变调用方看到的异常类型。
        return dialect.connect(*cargs, **cparams)

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
