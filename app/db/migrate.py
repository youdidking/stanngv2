"""Lightweight, additive schema migrations.

The project intentionally avoids a full Alembic setup for single-file
deployment simplicity, but schema changes are still versioned and idempotent.
Every step is written so that:

* running it twice is harmless,
* existing user data is never dropped,
* a failed step rolls back only that step.
"""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("armando.db.migrate")

# Version -> list of SQL statements (executed once, in order).
MIGRATIONS: dict[int, list[str]] = {
    1: [
        "CREATE INDEX IF NOT EXISTS ix_chat_members_warns ON chat_members (chat_id, warn_count)",
        "CREATE INDEX IF NOT EXISTS ix_warnings_active ON warnings (chat_id, active)",
    ],
    2: [
        "CREATE INDEX IF NOT EXISTS ix_mod_actions_expires ON moderation_actions (expires_at)",
        "CREATE INDEX IF NOT EXISTS ix_filters_chat_blocklist ON filters (chat_id, is_blocklist)",
    ],
    3: [
        "CREATE INDEX IF NOT EXISTS ix_notes_chat_name ON notes (chat_id, name)",
        "CREATE INDEX IF NOT EXISTS ix_ban_records_user ON ban_records (user_id, active)",
    ],
}

# SQLite has no "ADD COLUMN IF NOT EXISTS", so this step inspects the table
# first: a plain statement list would raise (and log a warning) on every
# single start-up once the columns exist.
async def _migration_4(engine: AsyncEngine) -> None:
    """Sticky ban, identity-change notices and auto-approved join requests."""
    await add_column(engine, "chat_settings", "sticky_ban",
                     "BOOLEAN NOT NULL DEFAULT 1")
    await add_column(engine, "chat_settings", "announce_name_change",
                     "BOOLEAN NOT NULL DEFAULT 1")
    await add_column(engine, "chat_settings", "announce_username_change",
                     "BOOLEAN NOT NULL DEFAULT 1")
    await add_column(engine, "chat_settings", "auto_approve_join_requests",
                     "BOOLEAN NOT NULL DEFAULT 0")


MIGRATIONS[4] = _migration_4


async def _migration_5(engine: AsyncEngine) -> None:
    """Group approval flag (opt-in gate for private deployments)."""
    await add_column(engine, "chats", "is_approved", "BOOLEAN NOT NULL DEFAULT 0")


MIGRATIONS[5] = _migration_5


async def _migration_6(engine: AsyncEngine) -> None:
    """Default exemption for messages posted under the group's own identity."""
    await add_column(engine, "chat_settings", "anonymous_sender_exempt",
                     "BOOLEAN NOT NULL DEFAULT TRUE")


MIGRATIONS[6] = _migration_6


async def _migration_7(engine: AsyncEngine) -> None:
    """Per-chat opt-out from the mention-all roster."""
    await add_column(engine, "chat_members", "mention_opt_out",
                     "BOOLEAN NOT NULL DEFAULT FALSE")


MIGRATIONS[7] = _migration_7


async def _migration_8(engine: AsyncEngine) -> None:
    """Configurable auto-deletion for bot reports and private broadcast results."""
    await add_column(engine, "chat_settings", "report_delete_after",
                     "INTEGER NOT NULL DEFAULT 12")
    await add_column(engine, "bot_settings", "broadcast_report_delete_after",
                     "INTEGER NOT NULL DEFAULT 12")


MIGRATIONS[8] = _migration_8


async def _migration_9(engine: AsyncEngine) -> None:
    """Store the selected capabilities on each scoped root grant."""
    await add_column(engine, "bot_root_grants", "permissions",
                     "JSON NOT NULL DEFAULT '[\"broadcast\"]'")


MIGRATIONS[9] = _migration_9


async def _migration_10(engine: AsyncEngine) -> None:
    """Store the independent per-group AI chat model selection."""
    await add_column(engine, "chat_settings", "ai_chat_model",
                     "VARCHAR(200) NOT NULL DEFAULT ''")


MIGRATIONS[10] = _migration_10


async def _migration_11(engine: AsyncEngine) -> None:
    """Enable optional private notifications for group leave events."""
    await add_column(engine, "chat_settings", "leave_notify_owner",
                     "BOOLEAN NOT NULL DEFAULT 0")


MIGRATIONS[11] = _migration_11


async def _migration_12(engine: AsyncEngine) -> None:
    """Add explicit per-group AI memory opt-in; preserve already selected AI chats."""
    await add_column(engine, "chat_settings", "ai_agent_enabled",
                     "BOOLEAN NOT NULL DEFAULT 0")
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE chat_settings SET ai_agent_enabled = TRUE "
            "WHERE ai_chat_model IS NOT NULL AND TRIM(ai_chat_model) <> ''"))


MIGRATIONS[12] = _migration_12


async def _migration_13(engine: AsyncEngine) -> None:
    """Replace only the shipped default welcome text; leave administrator templates intact."""
    if await check_missing_columns(engine, "chat_settings", ["welcome_text"]):
        return
    new_default = (
        "╭─── ༺ خوش‌آمدید ༻ ───╮\n"
        "🌷 سلام {mention} عزیز!\n\n"
        "🆔 شناسهٔ شما: <code>{user_id}</code>\n"
        "🔗 نام کاربری: <code>{username}</code>\n"
        "💬 گروه: <b>{chat_name}</b>\n"
        "🗓 زمان ورود: {date}، ساعت {time}\n\n"
        "📜 لطفاً پیش از گفتگو، قوانین گروه را مطالعه کنید.\n"
        "╰────────────────────╯")
    legacy_defaults = (
        "🌸 سلام {first_name} عزیز!\nبه گروه {chat_name} خوش آمدید.",
        "🌸 سلام {first_name} عزیز!\nبه گروه {chat_name} خوش آمدی.\n\n"
        "📜 لطفاً قوانین را مطالعه کن: /قوانین",
    )
    async with engine.begin() as conn:
        for old_default in legacy_defaults:
            await conn.execute(
                text("UPDATE chat_settings SET welcome_text = :new "
                     "WHERE welcome_text = :old"),
                {"new": new_default, "old": old_default})


MIGRATIONS[13] = _migration_13


async def _migration_14(engine: AsyncEngine) -> None:
    """Retire the configurable group TTL in favor of the fixed 20-second policy."""
    if await check_missing_columns(engine, "chat_settings", ["report_delete_after"]):
        return
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE chat_settings SET report_delete_after = 20 "
            "WHERE report_delete_after <> 20"))


MIGRATIONS[14] = _migration_14


async def _migration_15(engine: AsyncEngine) -> None:
    """Change the fixed global group-message TTL from 20 to 30 seconds."""
    if await check_missing_columns(engine, "chat_settings", ["report_delete_after"]):
        return
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE chat_settings SET report_delete_after = 30 "
            "WHERE report_delete_after <> 30"))


MIGRATIONS[15] = _migration_15

CURRENT_VERSION = max(MIGRATIONS) if MIGRATIONS else 0


async def _ensure_version_table(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " id INTEGER PRIMARY KEY,"
            " version INTEGER NOT NULL DEFAULT 0,"
            " applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        ))
        result = await conn.execute(text("SELECT COUNT(*) FROM schema_version"))
        if (result.scalar() or 0) == 0:
            await conn.execute(text("INSERT INTO schema_version (id, version, applied_at) VALUES (1, 0, CURRENT_TIMESTAMP)"))


async def _current_version(engine: AsyncEngine) -> int:
    await _ensure_version_table(engine)
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT version FROM schema_version WHERE id = 1"))
        return int(result.scalar() or 0)


async def run_migrations(engine: AsyncEngine) -> int:
    version = await _current_version(engine)
    applied = 0
    for target in sorted(v for v in MIGRATIONS if v > version):
        step = MIGRATIONS[target]
        try:
            if callable(step):
                await step(engine)
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE schema_version SET version = :v WHERE id = 1"),
                        {"v": target})
            else:
                async with engine.begin() as conn:
                    for statement in step:
                        await conn.execute(text(statement))
                    await conn.execute(
                        text("UPDATE schema_version SET version = :v WHERE id = 1"),
                        {"v": target})
            applied += 1
            logger.info("migration %s applied", target)
        except Exception as exc:  # noqa: BLE001 - never fail startup on a duplicate step
            logger.warning("migration %s skipped: %s", target, exc)
    return applied


async def check_missing_columns(engine: AsyncEngine, table: str, columns: list[str]) -> list[str]:
    """Helper used at runtime to add columns introduced after initial deploy."""

    def _inspect(sync_conn):
        inspector = inspect(sync_conn)
        if not inspector.has_table(table):
            return list(columns)
        existing = {c["name"] for c in inspector.get_columns(table)}
        return [c for c in columns if c not in existing]

    async with engine.begin() as conn:
        return await conn.run_sync(_inspect)


async def add_column(engine: AsyncEngine, table: str, column: str, ddl: str) -> bool:
    missing = await check_missing_columns(engine, table, [column])
    if not missing:
        return False
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        logger.info("column added: %s.%s", table, column)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not add %s.%s: %s", table, column, exc)
        return False


REGISTERED_RUNTIME_FIXUPS: list[Callable] = []
