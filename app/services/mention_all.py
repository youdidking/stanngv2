"""Mention known group members in notification-oriented batches.

The Bot API does not expose a complete member roster, so the service can only
mention users observed by :mod:`app.services.roster` (messages, joins, activity,
reactions and available administrators). Batches use five users each, the
conservative notification-oriented approach used by active mention bots.
``text_mention`` entities are built with UTF-16 offsets, as Telegram requires.
"""

from __future__ import annotations

import logging

from aiogram.types import MessageEntity
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.normalization import to_persian_digits
from ..db.models import ChatMemberState
from . import roster

logger = logging.getLogger("armando.mentionall")

# Send five real text_mention entities per message. Bot API docs do not promise
# a notification count; active mention-bot projects use five as the cautious
# batch size for individual mention alerts.
MENTIONS_PER_MESSAGE = 5
# Hard bounds for one invocation: at most 1000 known members in 200 messages.
MAX_MENTION_MESSAGES = 200
# Telegram's Bot FAQ says groups are limited to 20 bot messages/minute. A little
# over three seconds between batches leaves some room for ordinary bot traffic.
SEND_DELAY = 3.2

TELEGRAM_MESSAGE_LIMIT = 4096
# Do not try to ping an unbounded roster in one command.
MAX_MEMBERS = 1000

HEADER = "📣 فراخوانی اعضا"


def _utf16_len(text: str) -> int:
    """Telegram counts entity offsets and lengths in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def mention_text(name: str) -> str:
    """The visible text of a mention (kept short and on a single line)."""
    return " ".join((name or "").split())[:64] or "عضو"


def display_name(first_name: str | None, last_name: str | None,
                 username: str | None) -> str:
    parts = [p for p in ((first_name or "").strip(), (last_name or "").strip()) if p]
    if parts:
        return " ".join(parts)
    if username:
        return f"@{username}"
    return "عضو"


async def collect(session: AsyncSession, chat_id: int, *,
                  caller: tuple[int, str] | None = None,
                  bot=None, limit: int = MAX_MEMBERS) -> list[tuple[int, str]]:
    """Everybody the bot knows in ``chat_id`` as ``(user_id, display_name)``.

    ``caller`` is the admin who ran the command: the bot must not forget to
    mention them just because they never typed anything in the group.
    ``bot`` lets the roster also read the real administrator list.
    """
    try:
        limit = max(0, min(MAX_MEMBERS, int(limit)))
    except (TypeError, ValueError):
        limit = MAX_MEMBERS
    members = await roster.known_members(session, chat_id, bot=bot, limit=limit)
    result = await session.execute(
        select(ChatMemberState.user_id).where(
            ChatMemberState.chat_id == chat_id,
            ChatMemberState.mention_opt_out.is_(True)))
    excluded = {int(row[0]) for row in result.all()}
    members = [(uid, name) for uid, name in members if int(uid) not in excluded]
    if caller is not None and limit > 0:
        user_id, name = int(caller[0]), caller[1]
        if user_id > 0 and user_id not in excluded and not any(uid == user_id for uid, _ in members):
            # Prefer the command's author without exceeding the roster safety cap.
            if len(members) >= limit:
                members = members[:limit - 1]
            members.insert(0, (user_id, display_name(name, None, None)))
    return members[:limit]


def build_batches(members, *, per_message: int = MENTIONS_PER_MESSAGE,
                  max_messages: int = MAX_MENTION_MESSAGES
                  ) -> list[tuple[str, list[MessageEntity]]]:
    """Build entity-safe batches under both Telegram size and count limits.

    Names are grouped before entities are created, so no message is sliced
    after its offsets have been calculated. Returns ``(text, entities)`` pairs
    ready for ``send_message``.
    """
    try:
        per_message = max(1, min(MENTIONS_PER_MESSAGE, int(per_message)))
    except (TypeError, ValueError):
        per_message = MENTIONS_PER_MESSAGE
    try:
        max_messages = max(1, int(max_messages))
    except (TypeError, ValueError):
        max_messages = MAX_MENTION_MESSAGES

    max_candidates = min(MAX_MEMBERS, per_message * max_messages)
    prepared: list[tuple[int, str]] = []
    for item in members:
        try:
            user_id = int(item[0])
            name = mention_text(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if user_id <= 0:
            continue
        prepared.append((user_id, name))
        if len(prepared) >= max_candidates:
            break
    if not prepared:
        return []

    # Reserve the longest possible numbered header while packing. The actual
    # header is no longer than this, so every built message remains in-bounds.
    reserved_header = (
        f"{HEADER} ({to_persian_digits(f'{max_messages}/{max_messages}')})\n")
    header_units = _utf16_len(reserved_header)
    chunks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_units = header_units

    for user_id, visible in prepared:
        name_units = _utf16_len(visible)
        added_units = name_units + (1 if current else 0)
        if current and (
            len(current) >= per_message
            or current_units + added_units > TELEGRAM_MESSAGE_LIMIT
        ):
            chunks.append(current)
            if len(chunks) >= max_messages:
                break
            current = []
            current_units = header_units
            added_units = name_units

        if current_units + added_units > TELEGRAM_MESSAGE_LIMIT:
            logger.warning("Skipping unfit mention name for user_id=%s", user_id)
            continue
        current.append((user_id, visible))
        current_units += added_units
    else:
        if current and len(chunks) < max_messages:
            chunks.append(current)

    total = len(chunks)
    out: list[tuple[str, list[MessageEntity]]] = []
    for number, chunk in enumerate(chunks, start=1):
        head = HEADER
        if total > 1:
            head = f"{HEADER} ({to_persian_digits(f'{number}/{total}')})"
        text_parts = [f"{head}\n"]
        offset = _utf16_len(text_parts[0])
        entities: list[MessageEntity] = []

        for index, (user_id, visible) in enumerate(chunk):
            if index:
                text_parts.append(" ")
                offset += 1
            visible_length = _utf16_len(visible)
            entities.append(MessageEntity(
                type="text_mention", offset=offset, length=visible_length,
                user=TelegramUser(id=user_id, is_bot=False, first_name=visible)))
            text_parts.append(visible)
            offset += visible_length

        text = "".join(text_parts)
        if _utf16_len(text) > TELEGRAM_MESSAGE_LIMIT:
            # This should be unreachable due to the packing budget. Do not send
            # malformed text/entities if the header format changes later.
            logger.error("Skipping oversized mention batch (%s UTF-16 units)",
                         _utf16_len(text))
            continue
        out.append((text, entities))
    return out


def mentioned_ids(batches) -> list[int]:
    """Every user id mentioned in the batches (handy for logs and tests)."""
    ids: list[int] = []
    for _text, entities in batches:
        for entity in entities:
            user = getattr(entity, "user", None)
            if user is not None:
                ids.append(int(user.id))
    return ids
