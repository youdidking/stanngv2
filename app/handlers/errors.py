"""Global error handler: one failed update must never stop the bot."""

from __future__ import annotations

import logging
import traceback

from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent

from ..core.errors import notify_error_chat, persian_error

router = Router(name="errors")
logger = logging.getLogger("armando.errors")


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot) -> None:
    exception = event.exception
    update = event.update
    update_id = getattr(update, "update_id", None)
    logger.error(
        "update_failed update_id=%s error_type=%s error=%s",
        update_id, type(exception).__name__, exception,
        exc_info=(type(exception), exception, exception.__traceback__),
    )

    # Report critical (non-telegram) errors to the owner channel.
    module = type(exception).__module__ or ""
    if "aiogram" not in module:
        tb = "".join(traceback.format_exception(type(exception), exception,
                                                exception.__traceback__))[-1500:]
        await notify_error_chat(bot, f"⚠️ <b>{type(exception).__name__}</b>\n"
                                     f"<code>{str(exception)[:300]}</code>\n\n"
                                     f"<pre>{tb}</pre>")

    # Try to inform the user without ever raising again.
    try:
        message = None
        if update is not None:
            message = getattr(update, "message", None) or getattr(update, "callback_query", None)
            if message is not None and hasattr(message, "message"):
                message = message.message
        if message is not None and hasattr(message, "chat"):
            text = persian_error(exception) or "⚠️ خطایی رخ داد."
            await bot.send_message(chat_id=message.chat.id, text=text,
                                   parse_mode=ParseMode.HTML)
    except Exception:  # noqa: BLE001
        logger.debug("could not notify user about the error", exc_info=True)
