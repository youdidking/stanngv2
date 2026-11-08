"""Personal commands (custom triggers) and magic sticker / GIF triggers."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.normalization import normalize_text
from ..db.models import MagicTrigger, PersonalCommand

logger = logging.getLogger("armando.personal")

_cooldowns: dict[tuple[int, str], float] = {}


def normalize_trigger(text: str) -> str:
    return normalize_text(text or "", mode="command").strip()


def on_cooldown(chat_id: int, trigger: str, cooldown: int) -> bool:
    if cooldown <= 0:
        return False
    now = time.monotonic()
    key = (chat_id, trigger)
    last = _cooldowns.get(key, 0.0)
    if now - last < cooldown:
        return True
    _cooldowns[key] = now
    return False


def prune_cooldowns(max_age: float = 600.0) -> int:
    now = time.monotonic()
    stale = [k for k, v in _cooldowns.items() if now - v > max_age]
    for key in stale:
        _cooldowns.pop(key, None)
    return len(stale)


# --------------------------------------------------------------------------- #
# Personal commands
# --------------------------------------------------------------------------- #
async def add_command(session: AsyncSession, chat_id: int, trigger: str, *,
                      content_type: str = "text", text: str = "", file_id: str | None = None,
                      caption: str | None = None, buttons: list | None = None,
                      aliases: list[str] | None = None, cooldown: int = 0,
                      created_by: int | None = None) -> PersonalCommand:
    from .chat_state import ensure_chat

    normalized = normalize_trigger(trigger)
    if not normalized:
        raise ValueError("trigger is required")
    await ensure_chat(session, chat_id)
    result = await session.execute(
        select(PersonalCommand).where(PersonalCommand.chat_id == chat_id,
                                      PersonalCommand.trigger == normalized)
    )
    command = result.scalar_one_or_none()
    if command is None:
        command = PersonalCommand(chat_id=chat_id, trigger=normalized,
                                  created_at=datetime.utcnow())
    command.content_type = content_type
    command.text = text or ""
    command.file_id = file_id
    command.caption = caption
    command.buttons = buttons or []
    command.aliases = [normalize_trigger(a) for a in (aliases or [])]
    command.cooldown = int(cooldown or 0)
    command.enabled = True
    command.created_by = created_by
    session.add(command)
    await session.flush()
    return command


async def remove_command(session: AsyncSession, chat_id: int, trigger: str) -> bool:
    normalized = normalize_trigger(trigger)
    result = await session.execute(
        select(PersonalCommand).where(PersonalCommand.chat_id == chat_id,
                                      PersonalCommand.trigger == normalized)
    )
    command = result.scalar_one_or_none()
    if command is None:
        # try aliases
        result = await session.execute(
            select(PersonalCommand).where(PersonalCommand.chat_id == chat_id)
        )
        for candidate in result.scalars().all():
            if normalized in (candidate.aliases or []):
                await session.delete(candidate)
                await session.flush()
                return True
        return False
    await session.delete(command)
    await session.flush()
    return True


async def get_command(session: AsyncSession, chat_id: int, text: str) -> PersonalCommand | None:
    normalized = normalize_trigger(text)
    if not normalized:
        return None
    result = await session.execute(
        select(PersonalCommand).where(PersonalCommand.chat_id == chat_id,
                                      PersonalCommand.enabled.is_(True))
    )
    for command in result.scalars().all():
        if command.trigger == normalized or normalized in (command.aliases or []):
            return command
    return None


async def list_commands(session: AsyncSession, chat_id: int) -> list[PersonalCommand]:
    result = await session.execute(
        select(PersonalCommand).where(PersonalCommand.chat_id == chat_id)
        .order_by(PersonalCommand.trigger)
    )
    return list(result.scalars().all())


async def clear_commands(session: AsyncSession, chat_id: int) -> int:
    result = await session.execute(delete(PersonalCommand).where(PersonalCommand.chat_id == chat_id))
    await session.flush()
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# Magic triggers
# --------------------------------------------------------------------------- #
async def add_magic(session: AsyncSession, chat_id: int, *, trigger_type: str,
                    file_unique_id: str, content_type: str = "text", text: str = "",
                    file_id: str | None = None, caption: str | None = None,
                    buttons: list | None = None, created_by: int | None = None) -> MagicTrigger:
    from .chat_state import ensure_chat

    await ensure_chat(session, chat_id)
    result = await session.execute(
        select(MagicTrigger).where(MagicTrigger.chat_id == chat_id,
                                   MagicTrigger.trigger_type == trigger_type,
                                   MagicTrigger.file_unique_id == file_unique_id)
    )
    trigger = result.scalar_one_or_none()
    if trigger is None:
        trigger = MagicTrigger(chat_id=chat_id, trigger_type=trigger_type,
                               file_unique_id=file_unique_id,
                               created_at=datetime.utcnow())
    trigger.content_type = content_type
    trigger.text = text or ""
    trigger.file_id = file_id
    trigger.caption = caption
    trigger.buttons = buttons or []
    trigger.created_by = created_by
    session.add(trigger)
    await session.flush()
    return trigger


async def get_magic(session: AsyncSession, chat_id: int, trigger_type: str,
                    file_unique_id: str) -> MagicTrigger | None:
    from .chat_state import ensure_chat

    await ensure_chat(session, chat_id)
    result = await session.execute(
        select(MagicTrigger).where(MagicTrigger.chat_id == chat_id,
                                   MagicTrigger.trigger_type == trigger_type,
                                   MagicTrigger.file_unique_id == file_unique_id)
    )
    return result.scalar_one_or_none()


async def remove_magic(session: AsyncSession, chat_id: int, trigger_id: int) -> bool:
    trigger = await session.get(MagicTrigger, trigger_id)
    if trigger is None or trigger.chat_id != chat_id:
        return False
    await session.delete(trigger)
    await session.flush()
    return True


async def list_magic(session: AsyncSession, chat_id: int) -> list[MagicTrigger]:
    result = await session.execute(
        select(MagicTrigger).where(MagicTrigger.chat_id == chat_id)
        .order_by(MagicTrigger.id.desc())
    )
    return list(result.scalars().all())


def commands_text(commands: list[PersonalCommand], title: str = "⌨️ دستورات شخصی") -> str:
    if not commands:
        return f"{title}\n\nهیچ دستوری ثبت نشده است."
    lines = [title, ""]
    for index, command in enumerate(commands, start=1):
        extra = f" ({', '.join(command.aliases[:3])})" if command.aliases else ""
        lines.append(f"{index}. <code>{command.trigger}</code>{extra}")
    return "\n".join(lines)
