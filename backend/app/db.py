"""SQLite database (WAL mode) shared by the API thread and background worker threads."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite has no timezone-aware timestamps: store naive UTC, always return aware UTC."""
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


_engine = None
_SessionLocal = None
_init_lock = threading.Lock()
# SQLite allows one writer at a time. Serializing write transactions in-process avoids
# "database is locked" errors under concurrent worker threads (busy_timeout is the safety net).
WRITE_LOCK = threading.RLock()


def _on_connect(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=60000")
    cur.close()


def get_engine():
    global _engine, _SessionLocal
    with _init_lock:
        if _engine is None:
            _engine = create_engine(get_settings().database_url, future=True,
                                    connect_args={"check_same_thread": False, "timeout": 30})
            event.listen(_engine, "connect", _on_connect)
            _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine():
    global _engine, _SessionLocal
    with _init_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _SessionLocal = None


def SessionLocal():
    get_engine()
    return _SessionLocal()


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
