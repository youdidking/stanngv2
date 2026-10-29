"""Command target resolution: reply / @username / numeric id / mention / text."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.normalization import normalize_digits, normalize_text
from ..db.models import ChatMemberState

logger = logging.getLogger("armando.targeting")

# A username always contains a letter or an underscore - otherwise the token is
# a numeric user id and must not be looked up as a username.
USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")
ID_RE = re.compile(r"^\d{5,15}$")


@dataclass
class TargetResult:
    user_id: int | None = None
    first_name: str = ""
    last_name: str | None = None
    username: str | None = None
    source: str = "none"  # reply | username | id | mention | search | none
    error: str | None = None
    rest: list[str] = field(default_factory=list)  # remaining tokens after the target
    candidates: list[tuple[int, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.user_id is not None and not self.error

    @property
    def full_name(self) -> str:
        name = (self.first_name or "").strip()
        if self.last_name:
            name = f"{name} {self.last_name}".strip()
        return name or (f"@{self.username}" if self.username else "کاربر")

    def html(self) -> str:
        if not self.user_id:
            return self.full_name
        safe = (self.full_name or "کاربر").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<a href="tg://user?id={self.user_id}">{safe}</a>'


def html_user(user_id: int | None, name: str) -> str:
    if not user_id:
        safe = (name or "کاربر").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return safe
    safe = (name or "کاربر").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def _entity_mention(message: Message) -> TargetResult | None:
    """Resolve an inline ``text_mention`` entity (a mention without username)."""
    entities = message.entities or []
    text = message.text or message.caption or ""
    for entity in entities:
        if entity.type == "text_mention" and entity.user:
            user = entity.user
            result = TargetResult(user_id=user.id, first_name=user.first_name or "",
                                  last_name=user.last_name, username=user.username, source="mention")
            # Remove the mentioned text from the remaining tokens.
            mentioned = text[entity.offset: entity.offset + entity.length]
            rest = [t for t in (message.text or "").split() if normalize_text(t) != normalize_text(mentioned)]
            result.rest = rest[1:] if rest and normalize_text(rest[0]) else rest
            return result
    return None


async def _resolve_username(bot: Bot, session: AsyncSession, chat_id: int,
                            username: str) -> TargetResult | None:
    username = username.lstrip("@").lower()

    from ..db.models import User

    # 1) A user row we already know about.
    #    Telegram usernames are case-insensitive, and the Bot API offers no
    #    username -> id conversion, so the locally observed index is the only
    #    reliable source.  Exact match first (index friendly), then lowered.
    user = (await session.execute(
        select(User).where(User.username == username).limit(1))).scalars().first()
    if user is None:
        from sqlalchemy import func

        user = (await session.execute(
            select(User).where(func.lower(User.username) == username).limit(1))).scalars().first()
    if user:
        return TargetResult(user_id=user.id, first_name=user.first_name or "",
                            last_name=user.last_name, username=user.username, source="username")

    # 2) Resolve through the chat administrator list (one bounded API call).
    try:
        admins = await bot.get_chat_administrators(chat_id=chat_id)
        for admin in admins:
            u = admin.user
            if u.username and u.username.lower() == username:
                return TargetResult(user_id=u.id, first_name=u.first_name or "",
                                    last_name=u.last_name, username=u.username,
                                    source="username")
    except Exception:  # noqa: BLE001
        pass
    # 3) Public Telegram resolution (works for users with public usernames).
    try:
        chat = await bot.get_chat(chat_id=f"@{username}")
        if chat and chat.type == ChatType.PRIVATE:
            return TargetResult(user_id=chat.id, first_name=chat.first_name or "",
                                last_name=chat.last_name, username=chat.username, source="username")
    except Exception:  # noqa: BLE001
        logger.debug("username resolution failed for @%s", username)
    return None


async def _resolve_name(bot: Bot, session: AsyncSession, chat_id: int,
                        tokens: list[str]) -> list[tuple[int, str]]:
    """Best-effort name search among known members (used for ambiguous input)."""
    needle = normalize_text(" ".join(tokens), mode="command")
    if len(needle) < 3:
        return []
    from ..db.models import User

    # One joined query: the old code fetched the states and then issued a
    # separate SELECT per member (500+ round trips on a busy group).
    result = await session.execute(
        select(User)
        .join(ChatMemberState, ChatMemberState.user_id == User.id)
        .where(ChatMemberState.chat_id == chat_id)
        .limit(500)
    )
    candidates: list[tuple[int, str]] = []
    for user in result.scalars().all():
        full = normalize_text(f"{user.first_name or ''} {user.last_name or ''} {user.username or ''}",
                              mode="command")
        if needle in full:
            candidates.append((user.id, f"{user.first_name or ''} {user.last_name or ''}".strip()
                               or f"@{user.username}" or str(user.id)))
        if len(candidates) >= 5:
            break
    return candidates


async def resolve_target(bot: Bot, session: AsyncSession, message: Message,
                         args: list[str], *, allow_self: bool = False) -> TargetResult:
    """Resolve the moderation target of a command.

    Order: reply → text_mention entity → @username → numeric id → name search.
    """
    args = [a for a in (args or []) if a.strip()]

    # A) Reply
    reply = message.reply_to_message
    if reply is not None:
        if getattr(reply, "sender_chat", None) is not None:
            return TargetResult(error="❌ پیام انتخاب‌شده متعلق به یک کانال است و هدف معتبری ندارد.")
        user = reply.from_user
        if user is None:
            return TargetResult(error="❌ فرستنده پیام قابل تشخیص نیست.")
        if user.is_bot and user.id == bot.id:
            return TargetResult(error="⛔️ ربات هدف معتبری برای این عملیات نیست.")
        return TargetResult(user_id=user.id, first_name=user.first_name or "",
                            last_name=user.last_name, username=user.username,
                            source="reply", rest=list(args))

    # B) Inline mention entity
    if message.entities and args:
        entity_result = _entity_mention(message)
        if entity_result and entity_result.user_id:
            entity_result.rest = args[1:]
            return entity_result

    if not args:
        return TargetResult(error=("⚠️ لطفاً روی پیام کاربر ریپلای کنید، "
                                   "یا نام کاربری/شناسه عددی او را بنویسید.\n"
                                   "مثال: <code>بن @username</code>"))

    first = args[0]
    normalized_first = normalize_digits(first, to="ascii")

    # C) numeric id (checked first: a pure digit token is never a username)
    if ID_RE.match(normalized_first):
        user_id = int(normalized_first)
        from ..db.models import User

        user = await session.get(User, user_id)
        if user:
            return TargetResult(user_id=user.id, first_name=user.first_name or "",
                                last_name=user.last_name, username=user.username,
                                source="id", rest=args[1:])
        try:
            member = await bot.get_chat_member(chat_id=message.chat.id, user_id=user_id)
            u = member.user
            return TargetResult(user_id=u.id, first_name=u.first_name or "",
                                last_name=u.last_name, username=u.username,
                                source="id", rest=args[1:])
        except Exception:  # noqa: BLE001
            return TargetResult(user_id=user_id, first_name=f"کاربر {user_id}",
                                source="id", rest=args[1:])

    # D) @username
    match = USERNAME_RE.match(first)
    if match:
        resolved = await _resolve_username(bot, session, message.chat.id, match.group(1))
        if resolved:
            resolved.rest = args[1:]
            return resolved
        return TargetResult(error=f"❌ کاربر <code>@{match.group(1)}</code> پیدا نشد.")

    # E) ambiguous: name search
    candidates = await _resolve_name(bot, session, message.chat.id, args[:2])
    if len(candidates) == 1:
        user_id, name = candidates[0]
        return TargetResult(user_id=user_id, first_name=name, source="search", rest=args[2:])
    if len(candidates) > 1:
        lines = "\n".join(f"• {name} — <code>{user_id}</code>" for user_id, name in candidates)
        return TargetResult(
            error=("⚠️ چند کاربر با این نام پیدا شد. لطفاً دقیق‌تر مشخص کنید:\n"
                   f"{lines}\n\n یا روی پیام کاربر ریپلای کنید."),
            candidates=candidates,
        )
    return TargetResult(error="❌ کاربر هدف پیدا نشد. روی پیام او ریپلای کنید یا شناسه عددی/نام‌کاربری بدهید.")


async def resolve_target_reference(bot: Bot, session: AsyncSession, chat_id: int,
                                   reference: str) -> TargetResult:
    """Resolve one ID or username without using the command's replied message."""
    raw = (reference or "").strip()
    normalized = normalize_digits(raw, to="ascii")
    if ID_RE.fullmatch(normalized):
        user_id = int(normalized)
        from ..db.models import User

        user = await session.get(User, user_id)
        if user is not None:
            return TargetResult(user_id=user.id, first_name=user.first_name or "",
                                last_name=user.last_name, username=user.username, source="id")
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            user = member.user
            return TargetResult(user_id=user.id, first_name=user.first_name or "",
                                last_name=user.last_name, username=user.username, source="id")
        except Exception:  # noqa: BLE001 - Telegram may not reveal absent IDs
            return TargetResult(user_id=user_id, first_name=f"کاربر {user_id}", source="id")

    match = USERNAME_RE.fullmatch(raw)
    if match:
        username = match.group(1)
        resolved = await _resolve_username(bot, session, chat_id, username)
        if resolved is not None:
            return resolved
        return TargetResult(error=f"کاربر @{username} در فهرست قابل‌شناسایی نیست.")
    return TargetResult(error="شناسه یا نام‌کاربری نامعتبر است.")


def extract_duration_and_reason(tokens: list[str]) -> tuple[int | None, str, list[str]]:
    """Split remaining tokens into ``(duration_seconds, reason, rest)``."""
    from ..core.duration import parse_duration

    duration: int | None = None
    rest: list[str] = []
    for token in tokens:
        if duration is None:
            parsed = parse_duration(token)
            if parsed is not None:
                duration = parsed
                continue
        rest.append(token)
    return duration, " ".join(rest).strip(), rest
