"""Background scheduler: due jobs, expiry handling and cleanup loops.

Runs inside the bot process (no external worker required) and is safe for
multi-instance deployments for the read-only jobs (statistics), while
destructive expiries are idempotent.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import config
from ..core import cache
from ..core import ratelimit
from ..core.errors import safe_restrict, safe_unban
from ..db import session_scope
from ..db.models import (
    BanRecord,
    Chat,
    ChatMemberState,
    ChatSettings,
    ModerationAction,
    ScheduledMessage,
    Warning,
)
from . import antiflood, antiraid, captcha, nightmode, personal, stats

logger = logging.getLogger("armando.scheduler")


def compute_next_run(message_row: ScheduledMessage, now: datetime | None = None) -> datetime | None:
    """Compute the next execution time for a scheduled message."""
    from ..core.timeutils import next_occurrence

    now = now or datetime.utcnow()
    if message_row.repeat_seconds:
        return now + timedelta(seconds=int(message_row.repeat_seconds))
    cron = (message_row.cron or "").strip()
    if not cron:
        return None
    parts = cron.split(":")
    try:
        if parts[0] == "weekly" and len(parts) == 3:
            weekday = int(parts[1])
            hour, minute = (int(x) for x in parts[2].split(":"))
            from ..core.timeutils import tehran_now

            local_now = tehran_now()
            candidate = next_occurrence(hour, minute, local_now)
            while candidate.weekday() != weekday:
                candidate = next_occurrence(hour, minute, candidate)
            return candidate.astimezone(timezone.utc).replace(tzinfo=None)
        # "HH:MM" daily
        hour, minute = (int(x) for x in parts[0].split(":"))
        from ..core.timeutils import tehran_now

        candidate = next_occurrence(hour, minute, tehran_now())
        return candidate.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        logger.warning("invalid cron expression: %s", cron)
        return None


class SchedulerService:
    """Periodic maintenance loop."""

    def __init__(self, bot: Bot, tick: int | None = None) -> None:
        self.bot = bot
        self.tick = tick or config.settings.scheduler_tick
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._deleted_scan_task: asyncio.Task | None = None
        self._deleted_scanner = None
        self._last_snapshot_day: dict[int, str] = {}
        self._night_window_states: dict[int, bool] = {}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="armando-scheduler")
        await self._start_deleted_account_scanner()
        logger.info("زمان‌بند فعال شد؛ فاصلهٔ بررسی %s ثانیه است.", self.tick)

    async def _start_deleted_account_scanner(self) -> None:
        cfg = config.settings
        if not (cfg.telegram_api_id and cfg.telegram_api_hash):
            logger.info("اسکن MTProto غیرفعال است؛ TELEGRAM_API_ID و TELEGRAM_API_HASH تنظیم نشده‌اند.")
            return
        from .deleted_accounts import DeletedAccountScanner

        scanner = None
        try:
            scanner = DeletedAccountScanner(
                self.bot, api_id=cfg.telegram_api_id, api_hash=cfg.telegram_api_hash,
                bot_token=cfg.bot_token,
                session_path=config.DATA_DIR / "armando-mtproto.session",
                session_string=cfg.telegram_session_string,
                max_channel_pages=cfg.deleted_account_max_pages)
            await asyncio.wait_for(scanner.connect(), timeout=25)
        except Exception as exc:  # noqa: BLE001 - MTProto is optional and must not stop the bot
            if scanner is not None:
                await scanner.disconnect()
            logger.warning("اتصال MTProto برقرار نشد؛ اسکن حساب‌های حذف‌شده غیرفعال ماند: %s",
                           exc.__class__.__name__)
            return
        self._deleted_scanner = scanner
        self._deleted_scan_task = asyncio.create_task(
            scanner.run_periodically(self._stop, cfg.deleted_account_scan_interval),
            name="armando-deleted-account-scan")
        logger.info("اسکن MTProto حساب‌های حذف‌شده فعال شد.")

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._task, self._deleted_scan_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = None
        self._deleted_scan_task = None
        if self._deleted_scanner is not None:
            await self._deleted_scanner.disconnect()
            self._deleted_scanner = None
        logger.info("زمان‌بند متوقف شد.")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                logger.exception("scheduler tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    # ---------------------------------------------------------------------- tick
    async def tick_once(self) -> None:
        async with session_scope() as session:
            await self._run_scheduled(session)
            await self._expire_captchas(session)
            await self._expire_restrictions(session)
            await self._notify_night_transitions(session)
            await self._expire_warnings(session)
            await self._snapshot(session)
            try:
                from .ai_memory import purge_expired_memory

                await purge_expired_memory(session)
            except Exception as exc:  # noqa: BLE001 - memory cleanup must not stop other jobs
                logger.warning("AI group-memory cleanup failed: %s", exc.__class__.__name__)
        self._cleanup()

    # ---------------------------------------------------------------------- jobs
    async def _run_scheduled(self, session: AsyncSession) -> None:
        now = datetime.utcnow()
        result = await session.execute(
            select(ScheduledMessage).where(ScheduledMessage.enabled.is_(True),
                                           ScheduledMessage.next_run_at.is_not(None),
                                           ScheduledMessage.next_run_at <= now)
        )
        rows = list(result.scalars().all())
        for row in rows:
            try:
                from .render import send_content

                await send_content(self.bot, row.chat_id, content_type="text",
                                   text=row.text, file_id=(row.media or {}).get("file_id"),
                                   caption=(row.media or {}).get("caption"),
                                   buttons=row.buttons)
                row.last_run_at = now
                row.run_count = int(row.run_count or 0) + 1
                row.next_run_at = compute_next_run(row, now)
                if row.next_run_at is None:
                    row.enabled = False
            except Exception as exc:  # noqa: BLE001
                logger.warning("scheduled message %s failed: %s", row.id, exc)
                row.next_run_at = now + timedelta(seconds=max(60, self.tick * 3))
        if rows:
            await session.flush()

    async def _expire_captchas(self, session: AsyncSession) -> None:
        try:
            count = await captcha.expire_pending(self.bot, session)
            if count:
                logger.info("expired %s captcha sessions", count)
        except Exception as exc:  # noqa: BLE001
            logger.warning("captcha expiry job failed: %s", exc)

    async def _expire_restrictions(self, session: AsyncSession) -> None:
        now = datetime.utcnow()
        result = await session.execute(
            select(ModerationAction).where(ModerationAction.active.is_(True),
                                           ModerationAction.expires_at.is_not(None),
                                           ModerationAction.expires_at <= now,
                                           ModerationAction.action.in_(["temp_ban", "temp_mute"]))
        )
        for row in result.scalars().all():
            try:
                if row.action == "temp_ban":
                    ok = await safe_unban(self.bot, row.chat_id, row.target_id,
                                          only_if_banned=True, context="auto_unban")
                    if ok:
                        # Keep the auxiliary ban ledger in sync with the
                        # successfully expired moderation action.
                        await session.execute(
                            update(BanRecord).where(
                                BanRecord.chat_id == row.chat_id,
                                BanRecord.user_id == row.target_id,
                                BanRecord.scope == "chat",
                                BanRecord.active.is_(True),
                                BanRecord.until.is_not(None),
                                BanRecord.until <= now,
                            ).values(active=False))
                else:
                    from .moderation import UNMUTE_PERMISSIONS

                    ok = await safe_restrict(self.bot, row.chat_id, row.target_id,
                                             UNMUTE_PERMISSIONS, context="auto_unmute")
                if ok:
                    row.active = False
                    row.undone_at = now
                    row.undone_by = self.bot.id
                    state_result = await session.execute(
                        select(ChatMemberState).where(ChatMemberState.chat_id == row.chat_id,
                                                      ChatMemberState.user_id == row.target_id)
                    )
                    state = state_result.scalar_one_or_none()
                    if state is not None:
                        state.status = "left" if row.action == "temp_ban" else "member"
                        state.muted_until = None
                        state.restricted_until = None
                    cache.invalidate_user(row.chat_id, row.target_id)
                    logger.info("auto released %s for user %s in chat %s",
                                row.action, row.target_id, row.chat_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("expiry of action %s failed: %s", row.id, exc)
        await session.flush()

    async def _notify_night_transitions(self, session: AsyncSession) -> None:
        """Announce actual start/end edges once per running process."""
        result = await session.execute(
            select(Chat, ChatSettings)
            .join(ChatSettings, ChatSettings.chat_id == Chat.id)
            .where(Chat.is_active.is_(True))
        )
        current_chat_ids: set[int] = set()
        for chat, settings_obj in result.all():
            chat_id = int(chat.id)
            current_chat_ids.add(chat_id)
            active = nightmode.is_night(settings_obj)
            previous = self._night_window_states.get(chat_id)
            self._night_window_states[chat_id] = active
            if previous is None or previous == active:
                continue
            if active:
                text = "🌙 حالت شب آغاز شد؛ پیام‌های عادی طبق اقدام انتخاب‌شده کنترل می‌شوند."
            else:
                text = "🌤️ بازهٔ حالت شب پایان یافت؛ محدودیت‌های زمان‌دار شبانه آزاد می‌شوند."
            try:
                await self.bot.send_message(chat_id=chat_id, text=text)
            except Exception as exc:  # noqa: BLE001 - transition notices are best-effort
                logger.info("اعلان تغییر حالت شب در چت %s نرسید: %s",
                            chat_id, exc.__class__.__name__)

        for chat_id in set(self._night_window_states) - current_chat_ids:
            self._night_window_states.pop(chat_id, None)

    async def _expire_warnings(self, session: AsyncSession) -> None:
        now = datetime.utcnow()
        result = await session.execute(
            select(Warning).where(Warning.active.is_(True), Warning.expires_at.is_not(None),
                                  Warning.expires_at <= now)
        )
        rows = list(result.scalars().all())
        for row in rows:
            row.active = False
            row.removed_at = now
            row.removed_by = self.bot.id
            state_result = await session.execute(
                select(ChatMemberState).where(ChatMemberState.chat_id == row.chat_id,
                                              ChatMemberState.user_id == row.user_id)
            )
            state = state_result.scalar_one_or_none()
            if state is not None and int(state.warn_count or 0) > 0:
                state.warn_count = int(state.warn_count) - 1
            cache.invalidate_user(row.chat_id, row.user_id)
        if rows:
            await session.flush()
            logger.info("expired %s warnings", len(rows))

    async def _snapshot(self, session: AsyncSession) -> None:
        today = datetime.utcnow().date().isoformat()
        result = await session.execute(
            select(Chat).where(Chat.is_active.is_(True)).order_by(Chat.last_seen_at.desc()).limit(200)
        )
        for chat in result.scalars().all():
            if self._last_snapshot_day.get(chat.id) == today:
                continue
            try:
                await stats.snapshot(session, chat.id, self.bot)
                self._last_snapshot_day[chat.id] = today
            except Exception as exc:  # noqa: BLE001
                logger.debug("snapshot failed for %s: %s", chat.id, exc)
        await session.flush()

    # ------------------------------------------------------------------ cleanup
    def _cleanup(self) -> None:
        try:
            cache.cleanup_all()
            ratelimit.prune_all()
            antiflood.tracker.prune()
            antiraid.raid_tracker.prune()
            personal.prune_cooldowns()
            from ..services import entertainment

            entertainment.guess_game.cleanup()
        except Exception as exc:  # noqa: BLE001
            logger.debug("cleanup failed: %s", exc)


scheduler_service: SchedulerService | None = None


async def start_scheduler(bot: Bot) -> SchedulerService:
    global scheduler_service
    scheduler_service = SchedulerService(bot)
    await scheduler_service.start()
    return scheduler_service


async def stop_scheduler() -> None:
    global scheduler_service
    if scheduler_service is not None:
        await scheduler_service.stop()
        scheduler_service = None
