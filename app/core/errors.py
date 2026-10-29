"""Safe wrappers around the Telegram Bot API.

Every network call goes through :func:`safe_call`, which converts Telegram
errors into Persian, user presentable messages, honours ``RetryAfter`` and
never lets a single failed update crash the whole bot.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from aiogram.exceptions import (
    AiogramError,
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

from ..config import settings

logger = logging.getLogger("armando.telegram")
T = TypeVar("T")

DEFAULT_ERROR = "خطا در انجام عملیات"


def persian_error(exc: BaseException) -> str:
    """Map a Telegram/aiogram exception to a Persian message for the user."""
    if isinstance(exc, TelegramRetryAfter):
        return f"⏳ محدودیت تلگرام؛ لطفاً {exc.retry_after} ثانیه دیگر دوباره تلاش کنید."
    if isinstance(exc, TelegramForbiddenError):
        message = (exc.message or "").lower()
        if "user is deactivated" in message or "deactivated" in message:
            return "❌ حساب کاربر هدف غیرفعال است."
        if "bot was kicked" in message or "kicked" in message:
            return "❌ ربات از گروه اخراج شده است."
        if "not enough rights" in message or "need administrator" in message:
            return "❌ دسترسی لازم را ندارم؛ لطفاً سطح دسترسی ربات را بررسی کنید."
        return "❌ تلگرام اجازه این کار را نداد (دسترسی ربات ناکافی است)."
    if isinstance(exc, TelegramBadRequest):
        message = (exc.message or "").lower()
        if "not enough rights" in message or "administrator rights" in message:
            return "❌ دسترسی کافی ندارم. لطفاً دسترسی‌های ربات را در تنظیمات گروه بررسی کنید."
        if "user is an administrator" in message:
            return "❌ کاربر هدف یکی از مدیران گروه است و قابل محدودسازی نیست."
        if "user not found" in message or "user_id_invalid" in message:
            return "❌ کاربر مورد نظر پیدا نشد."
        if "message to delete not found" in message:
            return "❌ پیام مورد نظر پیدا نشد."
        if "message can't be deleted" in message or "too old" in message:
            return "❌ این پیام قدیمی است و قابل حذف نیست."
        if "chat not found" in message:
            return "❌ گروه پیدا نشد."
        if "message is not modified" in message:
            return ""
        if "can't remove chat owner" in message or "chat_admin_required" in message:
            return "❌ امکان اعمال محدودیت روی مالک گروه وجود ندارد."
        return f"❌ درخواست نامعتبر: {exc.message or 'خطای تلگرام'}"
    if isinstance(exc, TelegramNotFound):
        return "❌ منبع مورد نظر پیدا نشد."
    if isinstance(exc, TelegramNetworkError):
        return "❌ ارتباط با تلگرام برقرار نشد؛ لطفاً دوباره تلاش کنید."
    if isinstance(exc, TelegramUnauthorizedError):
        return "❌ توکن ربات نامعتبر است."
    if isinstance(exc, TelegramAPIError):
        return f"❌ خطای تلگرام: {exc.message or exc.__class__.__name__}"
    if isinstance(exc, AiogramError):
        return f"❌ {exc}"
    return DEFAULT_ERROR


async def safe_call(coro_factory: Callable[[], Awaitable[T]], *, default: Any = None,
                    retries: int = 1, log: bool = True, context: str = "") -> T | Any:
    """Await ``coro_factory()`` swallowing (and logging) Telegram failures.

    ``coro_factory`` must be a zero-argument callable returning a coroutine so
    that retries can re-create the request (a coroutine cannot be awaited twice).
    """
    attempt = 0
    last_exc: BaseException | None = None
    while attempt <= retries:
        try:
            return await coro_factory()
        except TelegramRetryAfter as exc:  # 429 - honour the requested delay once
            last_exc = exc
            wait = min(float(exc.retry_after), 30.0)
            if log:
                logger.warning("retry_after=%.1fs context=%s", wait, context or "-")
            await asyncio.sleep(wait)
            attempt += 1
        except TelegramBadRequest as exc:
            last_exc = exc
            message = (exc.message or "").lower()
            # These are routine (panel spam / already deleted messages), so they
            # must not fill the log in production.
            noisy = ("message is not modified" in message
                     or "message to delete not found" in message
                     or "message can't be deleted" in message)
            if log and not noisy:
                logger.warning("bad_request context=%s error=%s", context or "-", exc.message)
            elif log:
                logger.debug("bad_request context=%s error=%s", context or "-", exc.message)
            return default
        except (TelegramForbiddenError, TelegramNotFound, TelegramUnauthorizedError) as exc:
            last_exc = exc
            if log:
                logger.warning("%s context=%s error=%s", exc.__class__.__name__,
                               context or "-", exc.message)
            return default
        except TelegramNetworkError as exc:
            last_exc = exc
            if attempt >= retries:
                break
            await asyncio.sleep(1.5 * (attempt + 1))
            attempt += 1
        except TelegramAPIError as exc:
            last_exc = exc
            if log:
                logger.warning("telegram_api_error context=%s error=%s", context or "-", exc.message)
            return default
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never crash an update
            last_exc = exc
            if log:
                logger.exception("unexpected_error context=%s error=%s", context or "-", exc)
            return default
    if log and last_exc is not None:
        logger.warning("giving_up context=%s error=%s", context or "-", last_exc)
    return default


# --------------------------------------------------------------------------- #
# Convenience helpers
# --------------------------------------------------------------------------- #
async def safe_delete(bot, chat_id: int, message_id: int, context: str = "") -> bool:
    if not message_id:
        return False
    result = await safe_call(lambda: bot.delete_message(chat_id=chat_id, message_id=message_id),
                             default=False, context=context or "delete_message")
    return bool(result)


_DELETE_TASKS: set[asyncio.Task] = set()


def schedule_delete(bot, chat_id: int, message_id: int, delay: int,
                    context: str = "auto_delete") -> asyncio.Task | None:
    """Schedule safe deletion of one bot message; a delay of zero disables it."""
    try:
        seconds = int(delay)
    except (TypeError, ValueError):
        return None
    if seconds <= 0 or not message_id:
        return None
    seconds = min(seconds, 3600)

    async def _delete_later() -> None:
        await asyncio.sleep(seconds)
        await safe_delete(bot, chat_id, message_id, context=context)

    task = asyncio.create_task(_delete_later())
    _DELETE_TASKS.add(task)
    task.add_done_callback(_DELETE_TASKS.discard)
    return task


async def safe_send(bot, chat_id: int, text: str, **kwargs):
    return await safe_call(lambda: bot.send_message(chat_id=chat_id, text=text, **kwargs),
                           context="send_message")


async def safe_edit(message, text: str, **kwargs):
    kwargs.setdefault("disable_web_page_preview", True)
    return await safe_call(lambda: message.edit_text(text, **kwargs), context="edit_message")


async def safe_edit_markup(message, reply_markup) -> None:
    await safe_call(lambda: message.edit_reply_markup(reply_markup=reply_markup),
                    context="edit_markup")


async def safe_answer(callback, text: str = "", show_alert: bool = False, **kwargs) -> None:
    await safe_call(lambda: callback.answer(text=text or None, show_alert=show_alert, **kwargs),
                    context="callback_answer")


async def safe_restrict(bot, chat_id: int, user_id: int, permissions, until_date=None,
                        context: str = "restrict") -> bool:
    result = await safe_call(
        lambda: bot.restrict_chat_member(chat_id=chat_id, user_id=user_id,
                                         permissions=permissions, until_date=until_date),
        default=False, context=context)
    return bool(result)


async def safe_ban(bot, chat_id: int, user_id: int, until_date=None, revoke_messages: bool = False,
                   context: str = "ban") -> bool:
    result = await safe_call(
        lambda: bot.ban_chat_member(chat_id=chat_id, user_id=user_id, until_date=until_date,
                                    revoke_messages=revoke_messages),
        default=False, context=context)
    return bool(result)


async def safe_unban(bot, chat_id: int, user_id: int, only_if_banned: bool = True,
                     context: str = "unban") -> bool:
    result = await safe_call(
        lambda: bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=only_if_banned),
        default=False, context=context)
    return bool(result)


async def safe_ban_sender_chat(bot, chat_id: int, sender_chat_id: int,
                               context: str = "ban_sender_chat") -> bool:
    """Block a **channel** from posting in a supergroup.

    Useful when a spammer posts as one of their channels instead of their own
    account: banning the account leaves the channel inside the group.

    Caveats worth knowing (Bot API):
      * supergroups and channels only - basic groups raise an error,
      * it blocks the owner from posting as **any** of their channels,
      * the group's own linked channel cannot be banned.
    """
    if chat_id == sender_chat_id:        # a chat cannot ban itself
        return False
    result = await safe_call(
        lambda: bot.ban_chat_sender_chat(chat_id=chat_id, sender_chat_id=sender_chat_id),
        default=False, context=context, retries=0, log=False)
    return bool(result)


async def safe_unban_sender_chat(bot, chat_id: int, sender_chat_id: int,
                                 context: str = "unban_sender_chat") -> bool:
    """Lift a channel block again (see :func:`safe_ban_sender_chat`)."""
    result = await safe_call(
        lambda: bot.unban_chat_sender_chat(chat_id=chat_id, sender_chat_id=sender_chat_id),
        default=False, context=context, retries=0, log=False)
    return bool(result)


async def notify_error_chat(bot, text: str) -> None:
    """Send critical errors to the configured error log chat (never raises)."""
    target = settings.error_log_chat_id or settings.log_chat_id
    if not target:
        return
    try:
        await bot.send_message(chat_id=target, text=f"⚠️ خطای بحرانی ربات\n\n{text[:3500]}")
    except Exception:  # noqa: BLE001
        logger.debug("failed to notify error chat", exc_info=True)
