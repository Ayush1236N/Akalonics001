"""Async engine + session plumbing. SQLite by default, Postgres when DATABASE_URL is set."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession, async_sessionmaker,
                                    create_async_engine)

from config import settings
from models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        kwargs: dict = {"echo": False, "future": True}
        if settings.is_sqlite:
            # WAL + a generous busy timeout so concurrent in-process jobs don't
            # trip "database is locked" in the zero-infra dev path.
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)
        _engine = create_async_engine(settings.sqlalchemy_url, **kwargs)
        if settings.is_sqlite:
            from sqlalchemy import event

            @event.listens_for(_engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def reset_engine() -> None:
    """Used by tests when the DATABASE_URL changes between cases."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def init_models() -> None:
    """Create tables. Dev/SQLite convenience; production uses `alembic upgrade head`."""
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with sessionmaker()() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


async def ping() -> bool:
    from sqlalchemy import text
    try:
        async with sessionmaker()() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
