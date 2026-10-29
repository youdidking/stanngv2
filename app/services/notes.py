"""Notes system (text / media / buttons, retrievable by command or hashtag)."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import cache
from ..core.normalization import normalize_text
from ..db.models import Note

logger = logging.getLogger("armando.notes")


def normalize_note_name(name: str) -> str:
    return normalize_text(name or "", mode="command").strip().lstrip("#").strip()


async def get_note(session: AsyncSession, chat_id: int, name: str) -> Note | None:
    normalized = normalize_note_name(name)
    result = await session.execute(
        select(Note).where(Note.chat_id == chat_id, Note.name == normalized)
    )
    note = result.scalar_one_or_none()
    if note is None:
        # alias lookup
        result = await session.execute(select(Note).where(Note.chat_id == chat_id))
        for candidate in result.scalars().all():
            aliases = [normalize_note_name(a) for a in (candidate.aliases or [])]
            if normalized in aliases:
                return candidate
    return note


async def save_note(session: AsyncSession, chat_id: int, name: str, *, content_type: str = "text",
                    text: str = "", file_id: str | None = None, caption: str | None = None,
                    buttons: list | None = None, aliases: list[str] | None = None,
                    noformat: bool = False, created_by: int | None = None) -> Note:
    normalized = normalize_note_name(name)
    if not normalized:
        raise ValueError("note name is required")
    from .chat_state import ensure_chat

    await ensure_chat(session, chat_id)
    note = await get_note(session, chat_id, normalized)
    now = datetime.utcnow()
    if note is None:
        note = Note(chat_id=chat_id, name=normalized, created_at=now, created_by=created_by)
    note.content_type = content_type
    note.text = text or ""
    note.file_id = file_id
    note.caption = caption
    note.buttons = buttons or []
    note.aliases = [normalize_note_name(a) for a in (aliases or [])]
    note.noformat = noformat
    note.updated_at = now
    session.add(note)
    await session.flush()
    cache.filter_cache.delete(f"notes:{chat_id}")
    return note


async def delete_note(session: AsyncSession, chat_id: int, name: str) -> bool:
    normalized = normalize_note_name(name)
    result = await session.execute(
        select(Note).where(Note.chat_id == chat_id, Note.name == normalized)
    )
    note = result.scalar_one_or_none()
    if note is None:
        return False
    await session.delete(note)
    await session.flush()
    cache.filter_cache.delete(f"notes:{chat_id}")
    return True


async def list_notes(session: AsyncSession, chat_id: int) -> list[Note]:
    result = await session.execute(
        select(Note).where(Note.chat_id == chat_id).order_by(Note.name)
    )
    return list(result.scalars().all())


async def clear_notes(session: AsyncSession, chat_id: int) -> int:
    result = await session.execute(delete(Note).where(Note.chat_id == chat_id))
    await session.flush()
    cache.filter_cache.delete(f"notes:{chat_id}")
    return int(result.rowcount or 0)


def notes_text(notes: list[Note], title: str = "📝 لیست یادداشت‌ها") -> str:
    if not notes:
        return f"{title}\n\nهیچ یادداشتی ذخیره نشده است."
    lines = [title, ""]
    for index, note in enumerate(notes, start=1):
        kind = {"text": "📝", "photo": "🖼", "video": "🎞", "audio": "🎵", "voice": "🎙",
                "document": "📁", "sticker": "🎴", "animation": "🌀"}.get(note.content_type, "📝")
        lines.append(f"{index}. {kind} <code>#{note.name}</code>")
    lines.append("")
    lines.append("برای دریافت: <code>گرفتن قوانین</code> یا <code>#قوانین</code>")
    return "\n".join(lines)
