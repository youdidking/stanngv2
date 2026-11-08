"""Pin management helpers."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ParseMode

from ..core.errors import safe_call

logger = logging.getLogger("armando.pins")


async def pin_message(bot: Bot, chat_id: int, message_id: int, *, silent: bool = True) -> bool:
    result = await safe_call(
        lambda: bot.pin_chat_message(chat_id=chat_id, message_id=message_id,
                                     disable_notification=silent),
        default=None, context="pin")
    return result is not None


async def unpin_message(bot: Bot, chat_id: int, message_id: int | None = None) -> bool:
    if message_id:
        result = await safe_call(lambda: bot.unpin_chat_message(chat_id=chat_id,
                                                                message_id=message_id),
                                 default=None, context="unpin")
    else:
        result = await safe_call(lambda: bot.unpin_chat_message(chat_id=chat_id),
                                 default=None, context="unpin_last")
    return result is not None


async def unpin_all(bot: Bot, chat_id: int) -> bool:
    result = await safe_call(lambda: bot.unpin_all_chat_messages(chat_id=chat_id),
                             default=None, context="unpin_all")
    return result is not None


async def get_pinned(bot: Bot, chat_id: int):
    chat = await safe_call(lambda: bot.get_chat(chat_id=chat_id), default=None, context="get_chat")
    if chat is None:
        return None
    return getattr(chat, "pinned_message", None)


async def send_and_pin(bot: Bot, chat_id: int, text: str, *, silent: bool = True,
                       parse_mode: str = ParseMode.HTML):
    message = await safe_call(lambda: bot.send_message(chat_id=chat_id, text=text,
                                                       parse_mode=parse_mode,
                                                       disable_web_page_preview=True),
                              context="send_pin_text")
    if message is None:
        return None
    await pin_message(bot, chat_id, message.message_id, silent=silent)
    return message


async def edit_pinned_text(bot: Bot, chat_id: int, message_id: int, text: str) -> bool:
    result = await safe_call(lambda: bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                                           text=text, parse_mode=ParseMode.HTML,
                                                           disable_web_page_preview=True),
                             default=None, context="edit_pinned")
    return result is not None
