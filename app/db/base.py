"""Async database engine / session management (SQLite default, Postgres ready)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import settings
from .models import Base

logger = logging.getLogger("armando.db")

engine: AsyncEngine | None = None
SessionLocal: async_sessionmaker[AsyncSession] | None = None


def _sqlite_path() -> Path | None:
    url = settings.sqlalchemy_url
    if not url.startswith("sqlite"):
        return None
    marker = "///"
    idx = url.find(marker)
    if idx == -1:
        return None
    path = url[idx + 3:]
    if path.startswith("./") or path.startswith("/"):
        return Path(path)
    return Path(path)


def create_engine() -> AsyncEngine:
    url = settings.sqlalchemy_url
    kwargs: dict[str, object] = {"echo": settings.database_echo, "future": True}
    if url.startswith("sqlite"):
        from sqlalchemy.dialects.sqlite import aiosqlite  # noqa: F401
        db_file = _sqlite_path()
        if db_file:
            db_file.parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs["pool_size"] = max(2, settings.db_pool_size)
        kwargs["max_overflow"] = max(2, settings.db_pool_size)
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 1800
    return create_async_engine(url, **kwargs)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):  # pragma: no cover - driver hook
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
    except Exception:  # noqa: BLE001 - non-sqlite drivers
        return


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global SessionLocal
    if SessionLocal is None:
        SessionLocal = async_sessionmaker(bind=get_engine(), expire_on_commit=False,
                                          autoflush=False)
    return SessionLocal


def get_engine() -> AsyncEngine:
    global engine
    if engine is None:
        engine = create_engine()
    return engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Standalone session for background tasks / startup code."""
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI/aiogram style dependency yielding a session per update."""
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# Columns introduced after the first release: added idempotently so that
# existing PostgreSQL / SQLite databases keep working without a manual step.
RUNTIME_FIXUPS: tuple[tuple[str, str, str], ...] = (
    ("chat_settings", "ban_on_leave", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("chat_settings", "quick_leave_ban", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("chat_settings", "quick_leave_seconds", "INTEGER NOT NULL DEFAULT 10"),
)


async def _apply_runtime_fixups(engine) -> None:
    from .migrate import add_column

    for table, column, ddl in RUNTIME_FIXUPS:
        await add_column(engine, table, column, ddl)


async def init_db() -> None:
    """Create missing tables and apply pending migrations."""
    from .migrate import run_migrations

    eng = get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_migrations(eng)
    await _apply_runtime_fixups(eng)
    logger.info("database ready: %s", settings.sqlalchemy_url.split("://")[0])


async def health_check() -> bool:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("database health check failed: %s", exc)
        return False


async def dispose_engine() -> None:
    global engine, SessionLocal
    if engine is not None:
        await engine.dispose()
        engine = None
        SessionLocal = None
