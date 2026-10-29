"""Armando Group Manager - application entry point.

Supports long polling (default) and webhook mode (set ``USE_WEBHOOK=true``).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from .config import load_settings, settings
from .db.base import dispose_engine, init_db
from .handlers import build_routers
from .logging_setup import setup_logging
from .middlewares import (ChatTrackingMiddleware, DbSessionMiddleware,
                         GroupApprovalMiddleware, GroupMemoryMiddleware,
                         ThrottlingMiddleware)
from .core.message_lifecycle import GroupMessageAutoDeleteMiddleware
from .services.broadcast import MediaGroupCacheMiddleware
from .services.market import close_http_session
from .services.scheduler import start_scheduler, stop_scheduler

logger = logging.getLogger("armando")

BANNER = r"""
   _                                _         
  /_\   _ __ _ __ ___   __ _ _ __  | |_ ___   
 //_\\ | '_ \ '_ ` _ \ / _` | '_ \| __/ _ \  
/  _  \| | | | | | | | | (_| | | | | || (_) | 
\_/ \_/|_| |_|_| |_| |_|\__,_|_| |_|\__\___/  
        Armando Group Manager (Persian-first)
"""


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher(name="armando")
    # The approval gate runs before the per-update database middleware. It
    # creates a short-lived session for its check and lets membership updates
    # reach their handlers so bot removals are recorded correctly.
    dispatcher.update.outer_middleware(GroupApprovalMiddleware())
    dispatcher.update.outer_middleware(DbSessionMiddleware())
    dispatcher.update.outer_middleware(ThrottlingMiddleware())
    dispatcher.message.outer_middleware(MediaGroupCacheMiddleware())
    dispatcher.message.middleware(ChatTrackingMiddleware())
    dispatcher.message.middleware(GroupMemoryMiddleware())
    for observer in (dispatcher.edited_message, dispatcher.channel_post,
                     dispatcher.edited_channel_post):
        observer.middleware(ChatTrackingMiddleware())
        observer.middleware(GroupMemoryMiddleware())
    dispatcher.callback_query.middleware(ChatTrackingMiddleware())

    for router in build_routers():
        dispatcher.include_router(router)
    return dispatcher


async def register_bot_commands(bot: Bot) -> None:
    """Keep the slash command list minimal - the real UI is Persian text."""
    commands = [
        BotCommand(command="start", description="شروع کار با ربات"),
        BotCommand(command="help", description="راهنما"),
        BotCommand(command="panel", description="پنل مدیریت"),
        BotCommand(command="connect", description="اتصال خصوصی به گروه"),
        BotCommand(command="id", description="نمایش شناسه"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not register bot commands: %s", exc)


async def _poll(bot: Bot, dispatcher: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=settings.drop_pending_updates)
    logger.info("starting long polling")
    await dispatcher.start_polling(
        bot,
        allowed_updates=dispatcher.resolve_used_update_types(),
        drop_pending_updates=settings.drop_pending_updates,
        close_bot_session=False,
    )


async def _webhook(bot: Bot, dispatcher: Dispatcher) -> None:
    from aiohttp import web

    if not settings.webhook_url:
        raise RuntimeError("WEBHOOK_URL is required when USE_WEBHOOK=true")
    url = settings.webhook_url.rstrip("/") + settings.webhook_path
    await bot.set_webhook(url=url, secret_token=settings.webhook_secret,
                          drop_pending_updates=settings.drop_pending_updates,
                          allowed_updates=dispatcher.resolve_used_update_types())
    app = web.Application()
    handler = SimpleRequestHandler(dispatcher=dispatcher, bot=bot,
                                   secret_token=settings.webhook_secret)
    handler.register(app, path=settings.webhook_path)
    setup_application(app, dispatcher, bot=bot)

    async def health(_request) -> web.Response:
        return web.json_response({"status": "ok", "bot": "armando"})

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    logger.info("starting webhook server on %s:%s", settings.webapp_host, settings.webapp_port)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.webapp_host, port=settings.webapp_port)
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def run() -> None:
    load_settings()
    setup_logging()
    logger.info(BANNER)
    logger.info("environment=%s timezone=%s", settings.environment, settings.timezone)
    from . import config as app_config

    if app_config.settings.ai_chat_api_key and app_config.settings.ai_chat_base_url:
        logger.info("سرویس گفت‌وگوی هوشمند آماده است؛ مدیران هر گروه مدل جداگانه انتخاب می‌کنند.")

    await init_db()

    bot = Bot(token=settings.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                           link_preview_is_disabled=True,
                                           protect_content=False))
    bot.session.middleware.register(GroupMessageAutoDeleteMiddleware())
    dispatcher = build_dispatcher()
    await register_bot_commands(bot)
    await start_scheduler(bot)

    stop_event = asyncio.Event()

    def _signal_handler(*_: Any) -> None:
        logger.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:  # pragma: no cover - windows
            pass

    try:
        if settings.use_webhook:
            task = asyncio.create_task(_webhook(bot, dispatcher))
        else:
            task = asyncio.create_task(_poll(bot, dispatcher))
        await asyncio.wait({task, asyncio.create_task(stop_event.wait())},
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        logger.info("shutting down ...")
        await stop_scheduler()
        if settings.use_webhook:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except Exception:  # noqa: BLE001
                pass
        await close_http_session()
        await bot.session.close()
        await dispose_engine()
        logger.info("bye 👋")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("interrupted")
    except Exception as exc:  # noqa: BLE001
        logger.critical("fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
