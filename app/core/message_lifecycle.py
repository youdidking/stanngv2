"""Global outgoing-message lifecycle policies."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import Message, MessageId

from .errors import schedule_delete

logger = logging.getLogger("armando.message_lifecycle")
GROUP_BOT_MESSAGE_TTL = 30
_GROUP_TYPES = {"group", "supergroup"}
_CHAT_TYPE_CACHE_TTL = 15 * 60
_CHAT_TYPES: dict[int, tuple[str, float]] = {}
_PANEL_SEND_CONTEXT: ContextVar[bool] = ContextVar("armando_panel_message", default=False)
_PANEL_MESSAGE_LIMIT = 20000
_PANEL_MESSAGES: OrderedDict[tuple[int, int], None] = OrderedDict()
_PANEL_CALLBACK_PREFIXES = frozenset({
    "nav", "panel", "resetmenu", "reset", "set", "num", "dis",
    "lockpage", "lock", "lockact", "locknum", "cl", "aimodel",
    "aimemory", "ap", "rt", "lg", "cmd", "do",
})


def is_management_panel_markup(reply_markup: Any) -> bool:
    """Recognize a management keyboard when editing a panel after a restart."""
    rows = getattr(reply_markup, "inline_keyboard", None)
    if rows is None and isinstance(reply_markup, dict):
        rows = reply_markup.get("inline_keyboard")
    for row in rows or []:
        for button in row:
            data = getattr(button, "callback_data", None)
            if data is None and isinstance(button, dict):
                data = button.get("callback_data")
            if data and str(data).split(":", 1)[0] in _PANEL_CALLBACK_PREFIXES:
                return True
    return False


@contextmanager
def panel_message_context() -> Iterator[None]:
    """Mark an outgoing send as an interactive management panel."""
    token = _PANEL_SEND_CONTEXT.set(True)
    try:
        yield
    finally:
        _PANEL_SEND_CONTEXT.reset(token)


def remember_panel_message(chat_id: int, message_id: int) -> None:
    """Exclude this specific group message from the global auto-delete policy."""
    try:
        key = (int(chat_id), int(message_id))
    except (TypeError, ValueError):
        return
    _PANEL_MESSAGES[key] = None
    _PANEL_MESSAGES.move_to_end(key)
    while len(_PANEL_MESSAGES) > _PANEL_MESSAGE_LIMIT:
        _PANEL_MESSAGES.popitem(last=False)


def is_panel_message(chat_id: int, message_id: int) -> bool:
    key = (int(chat_id), int(message_id))
    if key not in _PANEL_MESSAGES:
        return False
    _PANEL_MESSAGES.move_to_end(key)
    return True


def clear_panel_messages() -> None:
    """Clear only the in-process panel exemption registry (used by tests)."""
    _PANEL_MESSAGES.clear()


def remember_chat_type(chat_id: int, chat_type: str) -> None:
    """Remember chat types observed in incoming Telegram updates."""
    try:
        _CHAT_TYPES[int(chat_id)] = (str(chat_type), time.monotonic())
    except (TypeError, ValueError):
        return


async def _is_group_chat(bot: Any, chat_id: int | str | None) -> tuple[bool, int | None]:
    if chat_id is None:
        return False, None
    try:
        numeric_id = int(chat_id)
    except (TypeError, ValueError):
        numeric_id = None
    if numeric_id is not None:
        cached = _CHAT_TYPES.get(numeric_id)
        if cached is not None and time.monotonic() - cached[1] < _CHAT_TYPE_CACHE_TTL:
            return cached[0] in _GROUP_TYPES, numeric_id
    try:
        chat = await bot.get_chat(chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001 - never break a successful send
        logger.debug("could not resolve chat type for auto-delete (%s)",
                     exc.__class__.__name__)
        return False, numeric_id
    actual_id = int(getattr(chat, "id", numeric_id or 0)) or numeric_id
    chat_type = str(getattr(chat, "type", ""))
    if actual_id is not None:
        remember_chat_type(actual_id, chat_type)
    return chat_type in _GROUP_TYPES, actual_id


class GroupMessageAutoDeleteMiddleware(BaseRequestMiddleware):
    """Delete ordinary bot-authored group/supergroup messages after a fixed TTL.

    Interactive management panels are explicitly exempt. Bot API methods
    returning a full ``Message`` are handled directly. Copy
    methods return ``MessageId`` objects, so their destination chat type is
    resolved from the observed-chat cache or one ``get_chat`` lookup. Private
    DMs are intentionally unaffected so owner/root reports remain available.
    """

    def __init__(self, delay: int = GROUP_BOT_MESSAGE_TTL) -> None:
        self.delay = max(1, min(int(delay), 3600))

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        if isinstance(result, Message):
            messages = [result]
        elif isinstance(result, (list, tuple)):
            # sendMediaGroup returns a list of Message objects; each item needs
            # the same policy as an ordinary sendMessage response.
            messages = [item for item in result if isinstance(item, Message)]
        else:
            messages = []
        if messages:
            for message in messages:
                chat = getattr(message, "chat", None)
                if chat is None:
                    continue
                remember_chat_type(chat.id, chat.type)
                if chat.type not in _GROUP_TYPES or not message.message_id:
                    continue
                if (_PANEL_SEND_CONTEXT.get()
                        or is_panel_message(int(chat.id), int(message.message_id))
                        or is_management_panel_markup(getattr(method, "reply_markup", None))):
                    remember_panel_message(int(chat.id), int(message.message_id))
                else:
                    schedule_delete(
                        bot, int(chat.id), int(message.message_id), self.delay,
                        context="global_group_message_ttl")
            return result

        # copyMessage returns one MessageId; copyMessages returns a list. These
        # are the Bot API send paths whose response omits the target chat.
        message_ids: list[int] = []
        if isinstance(result, MessageId):
            message_ids = [int(result.message_id)]
        elif isinstance(result, (list, tuple)):
            message_ids = [int(item.message_id) for item in result
                           if isinstance(item, MessageId) and getattr(item, "message_id", None)]
        if message_ids:
            destination = getattr(method, "chat_id", None)
            is_group, actual_chat_id = await _is_group_chat(bot, destination)
            if is_group and actual_chat_id is not None:
                for message_id in message_ids:
                    schedule_delete(
                        bot, actual_chat_id, message_id, self.delay,
                        context="global_group_message_ttl")
        return result
