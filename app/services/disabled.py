"""Per-chat command disabling (only features that are safe to turn off)."""

from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.normalization import normalize_text
from .chat_state import get_settings

logger = logging.getLogger("armando.disabled")

DISABLEABLE: dict[str, str] = {
    "games": "🎮 بازی‌ها",
    "joke": "😂 جوک",
    "fortune": "🔮 فال حافظ",
    "market": "💱 ارز و قیمت",
    "time": "🕒 تاریخ و ساعت",
    "afk": "💤 AFK",
    "report": "📢 گزارش",
    "stats": "📊 آمار",
    "translate": "🌐 ترجمه",
}

ALIASES: dict[str, str] = {
    "بازی": "games", "بازیها": "games", "بازی ها": "games",
    "جوک": "joke", "جوکها": "joke",
    "فال": "fortune", "فال حافظ": "fortune", "حافظ": "fortune",
    "ارز": "market", "قیمت": "market", "ارز دیجیتال": "market", "بورس": "market",
    "تاریخ": "time", "ساعت": "time", "زمان": "time",
    "ای اف کی": "afk", "آفک": "afk", "اف کی": "afk",
    "گزارش": "report", "گزارشها": "report",
    "آمار": "stats", "امار": "stats",
    "ترجمه": "translate",
}


def resolve_key(text: str) -> str | None:
    raw = normalize_text(text or "", mode="command").strip()
    if not raw:
        return None
    if raw in DISABLEABLE:
        return raw
    normalized_aliases = {normalize_text(k, mode="command"): v for k, v in ALIASES.items()}
    return normalized_aliases.get(raw)


async def is_disabled(session: AsyncSession, chat_id: int, key: str) -> bool:
    settings_obj = await get_settings(session, chat_id)
    disabled = list(settings_obj.disabled_commands or [])
    return key in disabled


async def disable(session: AsyncSession, chat_id: int, key: str) -> bool:
    if key not in DISABLEABLE:
        return False
    settings_obj = await get_settings(session, chat_id)
    disabled = list(settings_obj.disabled_commands or [])
    if key in disabled:
        return False
    disabled.append(key)
    settings_obj.disabled_commands = disabled
    await session.flush()
    from .chat_state import invalidate_settings

    invalidate_settings(chat_id)
    return True


async def enable(session: AsyncSession, chat_id: int, key: str) -> bool:
    settings_obj = await get_settings(session, chat_id)
    disabled = list(settings_obj.disabled_commands or [])
    if key not in disabled:
        return False
    disabled.remove(key)
    settings_obj.disabled_commands = disabled
    await session.flush()
    from .chat_state import invalidate_settings

    invalidate_settings(chat_id)
    return True


def disabled_text(keys: Iterable[str]) -> str:
    from ..core.normalization import to_persian_digits

    # Historical keys for retired features stay in storage but are not shown.
    keys = [key for key in keys if key in DISABLEABLE]
    if not keys:
        return "✅ هیچ بخشی غیرفعال نشده است."
    lines = ["🚫 <b>بخش‌های غیرفعال این گروه</b>", ""]
    for index, key in enumerate(keys, start=1):
        lines.append(f"{to_persian_digits(str(index))}. {DISABLEABLE.get(key, key)}")
    return "\n".join(lines)
