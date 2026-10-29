"""Telegram API calls that aiogram's generated stubs do not expose (yet).

``promoteChatMember`` accepts ``custom_title``, but the aiogram 3.31 stub for
:meth:`aiogram.Bot.promote_chat_member` does not, so we build the method
object by hand.  Everything else goes through the normal aiogram API.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import FormData
from aiogram import Bot
from aiogram.methods.base import TelegramMethod

logger = logging.getLogger("armando.telegram_extra")


class PromoteChatMemberWithTitle(TelegramMethod[bool]):
    """``promoteChatMember`` including the ``custom_title`` parameter."""

    __returning__ = bool
    __api_method__ = "promoteChatMember"

    chat_id: Any
    user_id: int
    custom_title: str | None = None
    is_anonymous: bool | None = None
    can_manage_chat: bool | None = None
    can_delete_messages: bool | None = None
    can_manage_video_chats: bool | None = None
    can_restrict_members: bool | None = None
    can_promote_members: bool | None = None
    can_change_info: bool | None = None
    can_invite_users: bool | None = None
    can_post_messages: bool | None = None
    can_edit_messages: bool | None = None
    can_pin_messages: bool | None = None
    can_manage_topics: bool | None = None
    can_post_stories: bool | None = None
    can_edit_stories: bool | None = None
    can_delete_stories: bool | None = None
    can_manage_direct_messages: bool | None = None
    can_manage_tags: bool | None = None
    can_send_welcome_messages: bool | None = None


def _form_value(value: Any) -> str:
    """Render a Python value the way the Bot API expects it inside form data."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def call_api_raw(bot: Bot, method_name: str, payload: dict[str, Any],
                       *, timeout: int = 30) -> Any | None:
    """Send a Bot API call keeping *falsy* values.

    aiogram builds the request as ``multipart/form-data`` and drops every value
    that is falsy (``False``, ``0``, ``""``).  A few calls need those values to
    be transmitted explicitly - e.g. ``setChatMemberTag`` with an empty tag
    (which *removes* the tag) or ``promoteChatMember`` with every right set to
    ``False`` (which demotes a member).  This helper posts the payload itself
    and returns ``result`` on success or ``None`` on failure.
    """
    try:
        session = await bot.session.create_session()
        url = bot.session.api.api_url(token=bot.token, method=method_name)
        form = FormData(quote_fields=False)
        for key, value in payload.items():
            if value is None:
                continue
            form.add_field(key, _form_value(value))
        async with session.post(url, data=form, timeout=timeout) as response:
            raw = await response.text()
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - network/JSON problems are not fatal
        logger.debug("raw api call %s failed: %s", method_name, exc)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        logger.info("telegram api %s rejected the call: %s", method_name,
                    (data or {}).get("description") if isinstance(data, dict) else raw[:120])
        return None
    return data.get("result")
