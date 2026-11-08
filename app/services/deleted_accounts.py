"""MTProto-only scanner for Telegram users marked with User.deleted."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from pathlib import Path

from aiogram import Bot
from sqlalchemy import select
from telethon import TelegramClient, functions, types, utils
from telethon.errors import ChannelInvalidError, FloodWaitError
from telethon.sessions import StringSession

from .. import config
from ..core.normalization import to_persian_digits
from ..db import session_scope
from ..db.models import Chat

logger = logging.getLogger("armando.deleted_accounts")
_PAGE_SIZE = 200
# The old hard cap of 50 pages silently stopped at roughly 10,000 members.
# 500 pages covers up to 100,000 participants while remaining configurable.
_MAX_CHANNEL_PAGES = 500
_PAGE_PAUSE_SECONDS = 0.1
_HEALTH_REPORT_COOLDOWN = 24 * 60 * 60


def is_deleted_mtproto_user(user: object) -> bool:
    """Use the protocol's explicit deleted flag; never infer from a display name."""
    return isinstance(user, types.User) and bool(getattr(user, "deleted", False))


class DeletedAccountScanner:
    def __init__(self, bot: Bot, *, api_id: int, api_hash: str,
                 bot_token: str, session_path: Path, session_string: str = "",
                 max_channel_pages: int = _MAX_CHANNEL_PAGES) -> None:
        self.bot = bot
        self.bot_token = bot_token
        self.session_string = (session_string or "").strip()
        self.max_channel_pages = max(1, min(int(max_channel_pages), 2000))
        self.is_bot_session: bool | None = None
        self._last_health_fingerprint = None
        self._last_health_report_at = 0.0

        if self.session_string:
            # User sessions can enumerate participants in chats where the
            # Telegram account itself has access; never write them to disk.
            session = StringSession(self.session_string)
        else:
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session = str(session_path)
        self.client = TelegramClient(
            session, api_id, api_hash,
            flood_sleep_threshold=60,
            connection_retries=1,
            request_retries=1,
        )

    async def connect(self) -> None:
        if self.session_string:
            # connect() avoids Telethon's interactive login prompt on a bad or
            # expired Railway Secret.
            await self.client.connect()
        else:
            await self.client.start(bot_token=self.bot_token)
        if not await self.client.is_user_authorized():
            raise RuntimeError("نشست MTProto مجاز نیست؛ شناسه/هش یا نشست را بررسی کنید")

        me = await self.client.get_me()
        self.is_bot_session = bool(getattr(me, "bot", False))
        if self.is_bot_session:
            if self.session_string:
                logger.warning("نشست MTProto ثبت‌شده به یک حساب ربات تعلق دارد؛ پوشش فهرست اعضا ممکن است محدود باشد.")
            else:
                if getattr(me, "id", None) != getattr(self.bot, "id", None):
                    raise RuntimeError("نشست MTProto ذخیره‌شده با BOT_TOKEN فعلی هم‌خوانی ندارد")
                logger.warning("اسکن با نشست Bot API اجرا می‌شود؛ دسترسی به فهرست اعضا را تلگرام تعیین می‌کند. برای پوشش بهتر TELEGRAM_SESSION_STRING را تنظیم کنید.")

    async def disconnect(self) -> None:
        try:
            await self.client.disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.info("بستن نشست MTProto ناموفق بود: %s", exc.__class__.__name__)

    async def run_periodically(self, stop_event: asyncio.Event, interval: int) -> None:
        while not stop_event.is_set():
            try:
                await self.scan_active_chats()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("اجرای اسکن حساب‌های حذف‌شده ناموفق بود: %s",
                               exc.__class__.__name__)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(3600, int(interval)))
            except asyncio.TimeoutError:
                continue

    async def scan_active_chats(self) -> None:
        async with session_scope() as session:
            result = await session.execute(
                select(Chat.id, Chat.type).where(
                    Chat.is_active.is_(True),
                    Chat.type.in_(("group", "supergroup", "channel")),
                ).order_by(Chat.id)
            )
            chats = [(int(row[0]), str(row[1])) for row in result.all()]

        failed_scans: list[tuple[int, str]] = []
        incomplete_chats: list[int] = []
        scanned = 0
        for chat_id, chat_type in chats:
            try:
                deleted_ids, incomplete = await self._deleted_user_ids(chat_id, chat_type)
            except asyncio.CancelledError:
                raise
            except FloodWaitError as exc:
                failed_scans.append((chat_id, "FloodWaitError"))
                logger.warning("تلگرام اسکن چت %s را به‌دلیل محدودیت نرخ متوقف کرد؛ %s ثانیه",
                               chat_id, int(exc.seconds))
                await asyncio.sleep(min(max(1, int(exc.seconds)), 300))
                continue
            except Exception as exc:  # noqa: BLE001 - continue after one inaccessible chat
                failed_scans.append((chat_id, exc.__class__.__name__))
                logger.warning("فهرست اعضای چت %s در دسترس نبود: %s",
                               chat_id, exc.__class__.__name__)
                await asyncio.sleep(0.05)
                continue

            scanned += 1
            if incomplete:
                incomplete_chats.append(chat_id)
            # Do not post a warning to a group on every partial scan. The owner
            # receives one consolidated private health report below.
            if deleted_ids:
                await self._ban_and_report(chat_id, deleted_ids, incomplete=incomplete)
            await asyncio.sleep(0.05)

        if failed_scans or incomplete_chats:
            await self._report_scan_health(
                scanned_chats=scanned,
                failed_scans=failed_scans,
                incomplete_chats=incomplete_chats,
            )

    async def _report_scan_health(self, *, scanned_chats: int,
                                  failed_scans: list[tuple[int, str]],
                                  incomplete_chats: list[int]) -> None:
        """Privately disclose inaccessible/partial chat scans, without secrets."""
        fingerprint = (
            tuple(sorted(failed_scans)),
            tuple(sorted(incomplete_chats)),
        )
        now = time.monotonic()
        if (fingerprint == self._last_health_fingerprint
                and now - self._last_health_report_at < _HEALTH_REPORT_COOLDOWN):
            return
        self._last_health_fingerprint = fingerprint
        self._last_health_report_at = now

        lines = [
            "⚠️ <b>گزارش پوشش اسکن حساب‌های حذف‌شده</b>",
            f"✅ چت‌های بررسی‌شده: {to_persian_digits(str(scanned_chats))}",
        ]
        if failed_scans:
            lines.append(f"❌ خطای دسترسی: {to_persian_digits(str(len(failed_scans)))} چت")
            for chat_id, error_name in failed_scans[:20]:
                hint = self._scan_error_hint(error_name)
                lines.append(
                    f"• <code>{to_persian_digits(str(chat_id))}</code> — {hint} "
                    f"(<code>{html.escape(error_name)}</code>)")
            if len(failed_scans) > 20:
                lines.append(f"… و {to_persian_digits(str(len(failed_scans) - 20))} چت دیگر.")
        if incomplete_chats:
            lines.append(f"⚠️ اسکن ناقص: {to_persian_digits(str(len(incomplete_chats)))} چت")
            shown = "، ".join(to_persian_digits(str(value)) for value in incomplete_chats[:20])
            lines.append(f"🆔 شناسه‌ها: <code>{shown}</code>")
        if self.is_bot_session:
            lines.extend([
                "ℹ️ نشست فعلی MTProto از نوع ربات است؛ دسترسی به فهرست اعضا به محدودیت‌ها و مجوزهای تلگرام بستگی دارد.",
                "برای پوشش بهتر، نشست یک حساب کاربریِ عضو کانال‌ها را با نام «TELEGRAM_SESSION_STRING» فقط در بخش Secrets راه‌اندازی Railway تنظیم کنید.",
            ])
        text = "\n".join(lines)
        owner_id = int(config.settings.owner_id or 0)
        if not owner_id:
            logger.warning("اسکن حساب‌های حذف‌شده ناقص/ناموفق بود؛ OWNER_ID تنظیم نشده است.")
            return
        try:
            await self.bot.send_message(
                chat_id=owner_id, text=text, parse_mode="HTML",
                disable_web_page_preview=True)
        except Exception as exc:  # noqa: BLE001 - owner may not have started the bot
            logger.warning("گزارش خصوصی اسکن به مالک نرسید: %s", exc.__class__.__name__)

    @staticmethod
    def _scan_error_hint(error_name: str) -> str:
        """Translate common Telegram access errors into actionable Persian."""
        if error_name in {"BotMethodInvalidError", "BotMethodInvalid"}:
            return "این روش برای نشست فعلی مجاز نیست"
        if error_name in {"ChatAdminRequiredError", "ChatAdminRequired"}:
            return "مجوز مدیر/مشاهدهٔ فهرست اعضا کافی نیست"
        if error_name in {"ChannelPrivateError", "ChannelPrivate"}:
            return "نشست MTProto به کانال خصوصی دسترسی ندارد"
        if error_name in {"ChannelInvalidError", "ChannelInvalid"}:
            return "شناسه یا دسترسی کانال معتبر نیست"
        if error_name in {"FloodWaitError", "FloodWait"}:
            return "تلگرام موقتاً محدودیت نرخ اعمال کرد"
        return "خواندن فهرست اعضا ناموفق بود"

    async def _resolve_input_channel(self, channel_id: int, *, refresh: bool = False):
        """Resolve this session's channel access hash before participant calls.

        Telethon's normal resolver first consults its entity/session cache.
        A stale access hash is refreshed explicitly; a zero hash is never sent
        to getParticipants.
        """
        if refresh:
            # A cached hash can become unusable after a channel migration or
            # access change. Re-bootstrap it through Telegram's entity lookup.
            result = await self.client(functions.channels.GetChannelsRequest(
                id=[types.InputChannel(channel_id=channel_id, access_hash=0)]
            ))
            entity = next((item for item in getattr(result, "chats", [])
                           if getattr(item, "id", None) == channel_id
                           and getattr(item, "access_hash", None) is not None), None)
            if entity is None:
                raise ValueError("کانال از نشست MTProto قابل resolve نیست")
            return types.InputChannel(channel_id=channel_id,
                                      access_hash=int(entity.access_hash))

        input_peer = await self.client.get_input_entity(types.PeerChannel(channel_id))
        return utils.get_input_channel(input_peer)

    def _participants_request(self, input_channel, offset: int):
        return functions.channels.GetParticipantsRequest(
            channel=input_channel,
            filter=types.ChannelParticipantsSearch(""),
            offset=offset,
            limit=_PAGE_SIZE,
            hash=0,
        )

    async def _deleted_user_ids(self, chat_id: int, chat_type: str) -> tuple[list[int], bool]:
        # The marked Bot API ID is authoritative for choosing Chat vs Channel.
        # Chat.type can be stale (for example, a group migrated to a supergroup).
        peer_id, peer_class = utils.resolve_id(chat_id)
        if peer_class is types.PeerChat:
            result = await self.client(
                functions.messages.GetFullChatRequest(chat_id=peer_id)
            )
            ids = sorted({int(user.id) for user in getattr(result, "users", [])
                          if is_deleted_mtproto_user(user)})
            return ids, False

        if peer_class is not types.PeerChannel:
            raise ValueError(f"شناسهٔ چت {chat_id} از نوع گروه یا کانال نیست")

        input_channel = await self._resolve_input_channel(peer_id)
        offset = 0
        deleted: set[int] = set()
        incomplete = False
        refreshed_hash = False

        max_pages = max(1, min(int(getattr(self, "max_channel_pages", _MAX_CHANNEL_PAGES)), 2000))
        for page in range(max_pages):
            try:
                result = await self.client(self._participants_request(input_channel, offset))
            except FloodWaitError:
                # Keep useful results from earlier pages and report that this
                # scan ended early rather than discarding them.
                incomplete = True
                break
            except ChannelInvalidError:
                if refreshed_hash:
                    raise
                # Retry once after explicitly fetching a current entity/access
                # hash through getChannels; never retry with hash=0 here.
                try:
                    input_channel = await self._resolve_input_channel(peer_id, refresh=True)
                    refreshed_hash = True
                    result = await self.client(
                        self._participants_request(input_channel, offset)
                    )
                except FloodWaitError:
                    incomplete = True
                    break

            for user in getattr(result, "users", []):
                if is_deleted_mtproto_user(user):
                    deleted.add(int(user.id))
            count = len(getattr(result, "participants", []) or [])
            try:
                total_count = int(getattr(result, "count", 0) or 0)
            except (TypeError, ValueError):
                total_count = 0
            next_offset = offset + count

            if total_count and next_offset >= total_count:
                break
            if count == 0 or count < _PAGE_SIZE:
                # If the API supplied a larger total, it returned only part of
                # the page; preserve the discovered deleted IDs and flag the gap.
                incomplete = bool(total_count and next_offset < total_count)
                break
            offset = next_offset
            if page + 1 == max_pages:
                incomplete = True
            else:
                await asyncio.sleep(_PAGE_PAUSE_SECONDS)
        return sorted(deleted), incomplete

    async def _ban_and_report(self, chat_id: int, user_ids: list[int], *,
                              incomplete: bool = False) -> dict[str, int]:
        banned: list[int] = []
        failed: list[int] = []
        for user_id in user_ids:
            try:
                # Omitting until_date creates a permanent ban. No permission is
                # added or elevated by this code; Telegram enforces bot rights.
                result = await self.bot.ban_chat_member(
                    chat_id=chat_id, user_id=user_id, revoke_messages=False)
                if result:
                    banned.append(user_id)
                else:
                    failed.append(user_id)
                    logger.info("تلگرام بن شناسهٔ حذف‌شده %s در چت %s را تأیید نکرد",
                                user_id, chat_id)
            except Exception as exc:  # noqa: BLE001
                failed.append(user_id)
                logger.info("بن دائمی شناسهٔ حذف‌شده %s در چت %s نشد: %s",
                            user_id, chat_id, exc.__class__.__name__)
            await asyncio.sleep(0.05)

        logger.info("نتیجهٔ بن حساب‌های حذف‌شده در چت %s: %s موفق، %s ناموفق",
                    chat_id, len(banned), len(failed))
        if banned:
            try:
                from .ban_reports import report_successful_ban

                async with session_scope() as session:
                    for user_id in banned:
                        try:
                            await report_successful_ban(
                                self.bot, session, chat_id=chat_id, target_id=user_id,
                                actor_id=self.bot.id,
                                reason="حساب در MTProto به‌عنوان حذف‌شده شناسایی شد.",
                                source="deleted_account")
                        except Exception as exc:  # noqa: BLE001 - one report cannot stop the batch
                            logger.warning("deleted-account ban report failed (%s)",
                                           exc.__class__.__name__)
            except Exception as exc:  # noqa: BLE001 - do not undo successful Telegram bans
                logger.warning("could not open session for deleted-account reports (%s)",
                               exc.__class__.__name__)
        lines = ["🧹 <b>بررسی حساب‌های حذف‌شده</b>"]
        if banned:
            lines.append(f"✅ بن دائمی شد: {to_persian_digits(str(len(banned)))} حساب")
            shown = "، ".join(to_persian_digits(str(value)) for value in banned[:20])
            lines.append(f"🆔 شناسه‌ها: <code>{shown}</code>")
            if len(banned) > 20:
                lines.append(f"… و {to_persian_digits(str(len(banned) - 20))} مورد دیگر.")
        if failed:
            lines.append(f"⚠️ بن انجام نشد: {to_persian_digits(str(len(failed)))} حساب")
            shown = "، ".join(to_persian_digits(str(value)) for value in failed[:20])
            lines.append(f"🆔 شناسه‌های ناموفق: <code>{shown}</code>")
            lines.append("دسترسی محدودسازی/مسدودکردن اعضا و وضعیت عضویت را بررسی کنید.")
        if incomplete:
            lines.append("⚠️ این نوبت همهٔ اعضا بررسی نشد؛ تلگرام یا سقف اسکن ادامهٔ فهرست را محدود کرد.")
        lines.append("ℹ️ فهرست اعضا ممکن است به‌علت محدودیت تلگرام کامل نباشد.")
        text = "\n".join(lines)
        try:
            await self.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode="HTML", disable_web_page_preview=True)
        except Exception as exc:  # noqa: BLE001 - channel posting rights can differ from ban rights
            logger.info("ارسال گزارش اسکن به چت %s ناموفق بود: %s",
                        chat_id, exc.__class__.__name__)
            owner_id = config.settings.owner_id
            if owner_id:
                try:
                    await self.bot.send_message(
                        chat_id=owner_id,
                        text=f"⚠️ گزارش پاکسازی چت <code>{chat_id}</code>\n{text}",
                        parse_mode="HTML", disable_web_page_preview=True)
                except Exception:  # noqa: BLE001 - owner may not have opened the bot
                    pass
        return {"banned": len(banned), "failed": len(failed)}
