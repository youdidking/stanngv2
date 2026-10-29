"""Middlewares: database session, throttling and light chat tracking."""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_send
from ..core.ratelimit import callback_limiter
from ..db.base import get_sessionmaker

logger = logging.getLogger("armando.middleware")


class DbSessionMiddleware(BaseMiddleware):
    """Provide a database session per update and commit when the handler ends."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if "session" in data:  # already provided (tests)
            return await handler(event, data)
        maker = get_sessionmaker()
        async with maker() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise
            finally:
                data.pop("session", None)


class ThrottlingMiddleware(BaseMiddleware):
    """Basic flood protection for the bot itself (callbacks and messages)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            key = (event.from_user.id if event.from_user else 0, "cb")
            allowed, retry = callback_limiter.check(key)
            if not allowed:
                try:
                    await event.answer(f"⏳ کمی صبر کنید ({int(retry)} ثانیه)", show_alert=False)
                except Exception:  # noqa: BLE001
                    pass
                return None
        return await handler(event, data)


class GroupMemoryMiddleware(BaseMiddleware):
    """Capture Telegram-delivered group/channel messages for opted-in seven-day memory."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            session: AsyncSession | None = data.get("session")
            if session is not None and event.chat.type in {"group", "supergroup", "channel"}:
                try:
                    from ..services.ai_memory import remember_group_message

                    await remember_group_message(session, event)
                except Exception as exc:  # noqa: BLE001 - memory must never block moderation
                    logger.warning("group AI-memory capture skipped (%s)",
                                   exc.__class__.__name__)
        return await handler(event, data)


class ChatTrackingMiddleware(BaseMiddleware):
    """Keep chat metadata fresh (cheap: only touches a few rows per update)."""

    def __init__(self) -> None:
        self._last_touch: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session: AsyncSession | None = data.get("session")
        message = event if isinstance(event, Message) else getattr(event, "message", None)
        if session is not None and message is not None and getattr(message, "chat", None):
            chat = message.chat
            try:
                from ..core.message_lifecycle import remember_chat_type

                remember_chat_type(chat.id, chat.type)
            except Exception:  # noqa: BLE001 - metadata cache must never block updates
                pass

            # Telegram never announces a profile edit, so every update is one
            # more chance to notice that somebody renamed themselves.
            actor = getattr(message, "from_user", None)
            if actor is not None and chat.type in {"group", "supergroup"}:
                try:
                    from ..services.identity_watch import watch_identity

                    bot = data.get("bot")
                    if bot is not None:
                        await watch_identity(bot, session, chat.id, actor)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("identity watch skipped: %s", exc)

            now = time.monotonic()
            last = self._last_touch.get(chat.id, 0.0)
            if now - last > 60:  # at most once a minute per chat
                self._last_touch[chat.id] = now
                try:
                    from ..db.models import Chat

                    row = await session.get(Chat, chat.id)
                    if row is not None:
                        from datetime import datetime

                        row.last_seen_at = datetime.utcnow()
                        row.is_active = True
                        if getattr(chat, "type", None):
                            row.type = chat.type
                        if chat.title and row.title != chat.title:
                            row.title = chat.title
                except Exception as exc:  # noqa: BLE001
                    logger.debug("chat tracking failed: %s", exc)
        return await handler(event, data)


def _update_chat(event: Any) -> Any:
    """Return the chat an update belongs to, or ``None``.

    An *update*-level middleware receives an :class:`Update`, not a Message, so
    the chat has to be dug out of whichever field carries it.
    """
    chat = getattr(event, "chat", None)
    if chat is not None:
        return chat
    for field in ("message", "edited_message", "channel_post", "my_chat_member",
                  "chat_member", "chat_join_request", "callback_query"):
        part = getattr(event, field, None)
        if part is None:
            continue
        chat = getattr(part, "chat", None)
        if chat is None:
            inner = getattr(part, "message", None)
            chat = getattr(inner, "chat", None) if inner is not None else None
        if chat is not None:
            return chat
    return None


class GroupApprovalMiddleware(BaseMiddleware):
    """Ignore groups the bot owner has not whitelisted yet.

    Only active when ``REQUIRE_GROUP_APPROVAL=true``; otherwise this is a
    no-op and the bot keeps behaving like the public multi-tenant bot it is.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from ..config import settings as app_settings

        if not app_settings.require_group_approval:
            return await handler(event, data)

        chat = _update_chat(event)
        if chat is None or chat.type not in {"group", "supergroup"}:
            return await handler(event, data)  # private chats are never gated

        # The bot's own membership updates must always reach the handler. In
        # particular, do not try to message a group after Telegram has removed
        # the bot from it.
        if getattr(event, "my_chat_member", None) is not None:
            return await handler(event, data)

        # This update-level middleware runs before DbSessionMiddleware, so it
        # opens a short-lived session only for the approval check.
        session: AsyncSession | None = data.get("session")
        own_session = session is None
        if own_session:
            try:
                session = get_sessionmaker()()
            except Exception as exc:  # noqa: BLE001
                logger.debug("approval gate could not open a session: %s", exc)
                return await handler(event, data)

        approved = False
        check_failed = False
        try:
            from ..db.models import Chat

            row = await session.get(Chat, chat.id)
            approved = bool(row is not None and row.is_approved)
        except Exception as exc:  # noqa: BLE001 - never break the bot for a gate
            logger.debug("approval check failed: %s", exc)
            check_failed = True
        finally:
            if own_session and session is not None:
                await session.close()

        if check_failed or approved:
            return await handler(event, data)

        # Not whitelisted yet: ask the owner (throttled) and remind the group
        # at most once an hour, then stay silent. Never reuse the just-closed
        # approval-check session: the previous version reopened it here and
        # left the resulting SQLite connection checked out until GC.
        bot = data.get("bot")
        if bot is not None and getattr(event, "message", None) is not None:
            from ..handlers.approval import maybe_notify_unapproved

            if own_session:
                try:
                    async with get_sessionmaker()() as notify_session:
                        await maybe_notify_unapproved(bot, notify_session, chat)
                        await notify_session.commit()
                except Exception as exc:  # noqa: BLE001 - the gate must not crash updates
                    logger.debug("ارسال اعلان تایید گروه ناموفق بود: %s", exc)
            else:
                await maybe_notify_unapproved(bot, session, chat)
        return None
