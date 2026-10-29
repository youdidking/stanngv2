"""Interactive panel: rendering, navigation, toggles and confirmations.

Callback data is never trusted: every callback re-resolves the actor from
Telegram and checks both the internal role and the real Telegram capability.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.types import (CallbackQuery, ChatMemberAdministrator, ChatMemberOwner,
                           Message)
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import cache
from ..core.errors import (safe_answer, safe_call, safe_delete, safe_edit,
                           safe_edit_markup, schedule_delete)
from ..core.normalization import to_persian_digits
from ..core.message_lifecycle import (
    GROUP_BOT_MESSAGE_TTL,
    is_management_panel_markup,
    is_panel_message,
    panel_message_context,
    remember_panel_message,
)
from ..db.models import (
    BotRole,
    Chat,
    ChatMemberState,
    Federation,
    FilterRule,
    GroupMessageMemory,
    Lock,
    ModerationAction,
    Note,
    PersonalCommand,
    Report,
    ScheduledMessage,
    TrustedUser,
    Warning,
)
from ..keyboards.factory import cb, confirm, danger, markup, parse_cb, primary, row
from ..keyboards.menus import (
    BOT_NAME,
    ROLE_LABELS,
    build_panel,
    confirm_text,
    help_text,
    with_creator,
)
from ..services import connection as connection_service
from ..services import disabled as disabled_service
from ..services import filters as filter_service
from ..services import locks as lock_service
from ..services import notes as note_service
from ..services import permissions
from ..services import personal as personal_service
from ..services import reports as report_service
from ..services import tags as tag_service
from ..services.chat_state import get_settings, get_settings_cached, invalidate_settings
from ..services.roles import level_of

router = Router(name="panel")
logger = logging.getLogger("armando.panel")

# Only configuration sections appear here. Every reset is isolated, confirmed,
# and leaves historical records (warnings, bans, filters, tags, reports) intact
# unless the specific setting being reset is itself the custom text/config.
RESET_SECTIONS: dict[str, tuple[str, str, str]] = {
    "security": ("security", "🔐 امنیت و ضداسپم",
                 "تنظیمات ضداسپم، ضدفلاود، ضد Raid، کپچا، فحاشی/پورن، تحلیل هوشمند و حالت شب به مقادیر پیش‌فرض برمی‌گردند؛ سوابق کاربران پاک نمی‌شوند."),
    "locks": ("locks", "🔒 قفل‌ها و خروج",
              "قفل‌های محتوایی خاموش، قفل تلگرام گروه باز، و تنظیمات قفل خروج پیش‌فرض می‌شوند. بن‌های موجود و سوابق تغییر نمی‌کنند."),
    "filters": ("filters", "📝 پیش‌فرض فیلتر",
                "فقط حالت و برخورد پیش‌فرض فیلتر بازنشانی می‌شود؛ فیلترها، یادداشت‌ها و پاسخ‌های ذخیره‌شده باقی می‌مانند."),
    "welcome": ("welcome", "👋 ورود و خروج",
                "متن خوشامد/خداحافظی، رسانه‌ها، دکمه‌ها و پاک‌سازی پیام‌های سرویسی به پیش‌فرض برمی‌گردند."),
    "warnings": ("warnings", "⚠️ تنظیمات اخطار",
                  "سقف، نوع و مدت اقدام و انقضای اخطار به پیش‌فرض برمی‌گردد؛ اخطارهای ثبت‌شده پاک نمی‌شوند."),
    "captcha": ("captcha", "🤖 کپچا",
                 "وضعیت و نوع چالش، مهلت، تعداد تلاش و اقدام پس از شکست به پیش‌فرض برمی‌گردند."),
    "antispam": ("antispam", "🚫 ضداسپم و ضدفلاود",
                  "فعال‌سازی، آستانه‌ها و نوع برخورد ضداسپم/ضدفلاود به پیش‌فرض برمی‌گردد."),
    "raid": ("raid", "🛡 ضد Raid",
             "آستانه، پنجره، اقدام و محدودسازی اعضای جدید به پیش‌فرض برمی‌گردد و وضعیت Raid فعلی لغو می‌شود."),
    "night": ("night", "🌙 حالت شب",
              "حالت شب، بازهٔ زمانی، اقدام و استثناهای آن به پیش‌فرض برمی‌گردند."),
    "ai": ("ai_chat", "💬 گفت‌وگوی هوشمند",
           "مدل و حافظهٔ ۷روزهٔ این گفتگو خاموش و حافظهٔ ذخیره‌شده و threadهای موقت پاک می‌شوند."),
    "rules": ("rules", "📜 قوانین",
              "متن سفارشی قوانین پاک و نمایش قوانین به حالت پیش‌فرض برمی‌گردد."),
    "reports": ("reports", "📢 گزارش‌ها",
                 "دریافت گزارش و اطلاع‌رسانی آن به پیش‌فرض برمی‌گردد؛ گزارش‌های ثبت‌شده باقی می‌مانند."),
    "trust": ("trust", "⭐️ کاربران ویژه",
              "استثناهای پیش‌فرض کاربران ویژه پاک می‌شود؛ عضویت کاربران ویژه و سوابق آن‌ها باقی می‌ماند."),
    "tags": ("tags", "🏷 تگ‌ها",
             "فقط اجازهٔ ویرایش خودکار تگ توسط اعضا به پیش‌فرض برمی‌گردد؛ تگ‌های موجود حذف نمی‌شوند."),
    "logs": ("logs", "📜 لاگ‌ها",
             "ثبت لاگ به پیش‌فرض برمی‌گردد و مقصد لاگ پاک می‌شود؛ پیام‌های قبلی حذف نمی‌شوند."),
    "disabled": ("disabled", "🚫 بخش‌های غیرفعال",
                 "همهٔ بخش‌ها و دستورهای قابل‌غیرفعال‌سازی دوباره فعال می‌شوند."),
}

RESET_DEFAULTS: dict[str, dict[str, object]] = {
    "warnings": {"warn_limit": 4, "warn_action": "mute", "warn_action_duration": None,
                 "warn_expire_days": 0, "warn_notify_user": True},
    "antispam": {"antispam_enabled": True, "antiflood_enabled": True,
                 "antiflood_count": 5, "antiflood_window": 3, "antiflood_action": "mute",
                 "antiflood_duration": None, "antispam_same_limit": 0,
                 "antispam_same_action": "warn", "mention_spam_limit": 0,
                 "mention_spam_action": "mute", "forward_spam_action": "none"},
    "captcha": {"captcha_enabled": False, "captcha_mode": "button", "captcha_timeout": 120,
                "captcha_max_attempts": 3, "captcha_fail_action": "kick",
                "captcha_delete_after": True},
    "raid": {"antiraid_enabled": False, "antiraid_threshold": 8,
             "antiraid_window": 60, "antiraid_action": "kick", "antiraid_lock_new": True,
             "antiraid_force_captcha": True, "raid_mode_until": None},
    "night": {"night_mode_enabled": False, "night_start": "01:00", "night_end": "06:00",
              "night_action": "mute", "night_bypass_roles": None},
    "filters": {"filter_default_action": "delete", "filter_default_mode": "word"},
    "rules": {"rules_text": "", "rules_enabled": True},
    "reports": {"report_enabled": True, "report_notify_admins": True,
                 "report_delete_after": 30},
    "trust": {"trust_bypass": None},
    "tags": {"tag_allow_user_edit": False},
    "logs": {"log_enabled": True, "log_chat_id": None},
    "disabled": {"disabled_commands": None},
}

_SECURITY_DEFAULTS = {
    **RESET_DEFAULTS["antispam"], **RESET_DEFAULTS["captcha"], **RESET_DEFAULTS["raid"],
    **RESET_DEFAULTS["night"],
    "anti_profanity_enabled": True, "anti_profanity_action": "delete_warn",
    "anti_porn_enabled": False, "anti_porn_action": "delete_ban",
    "anti_porn_strictness": 2, "ai_enabled": False, "ai_threshold": 0.75,
}

COMMAND_HINTS: dict[str, str] = {
    "ban": "🔨 برای بن کردن، روی پیام کاربر <b>ریپلای</b> کنید و بنویسید:\n<code>بن</code> یا <code>بن ۲روز تبلیغ</code>",
    "mute": "🔇 روی پیام کاربر ریپلای کنید و بنویسید:\n<code>سکوت</code> یا <code>سکوت ۳۰دقیقه</code>",
    "warn": "⚠️ روی پیام کاربر ریپلای کنید و بنویسید:\n<code>اخطار دلیل</code>",
    "kick": "👢 روی پیام کاربر ریپلای کنید و بنویسید:\n<code>کیک</code>",
    "unban": "🕊 بنویسید: <code>رفع بن @username</code> یا روی پیام کاربر ریپلای کنید.",
    "unmute": "🔊 روی پیام کاربر ریپلای کنید و بنویسید:\n<code>لغو سکوت</code>",
    "unwarn": "🗑 روی پیام کاربر ریپلای کنید و بنویسید:\n<code>کسر اخطار</code>",
    "resetwarn": "♻️ روی پیام کاربر ریپلای کنید و بنویسید:\n<code>صفر کردن اخطار</code>",
    "history": "📜 روی پیام کاربر ریپلای کنید و بنویسید:\n<code>تاریخچه</code>",
    "info": "ℹ️ روی پیام کاربر ریپلای کنید و بنویسید:\n<code>اطلاعات</code>",
    "me": "👤 بنویسید: <code>آمار من</code> یا <code>اطلاعات</code>",
    "afkhelp": "💤 برای رفتن به حالت AFK بنویسید: <code>برم افک دلیل</code>\nبرای بازگشت: <code>برگشتم</code>",
    "stats": "📊 بنویسید: <code>آمار</code>",
    "top": "🏆 بنویسید: <code>فعال‌ترین‌ها ۳۰</code>",
    "inactive": "💤 بنویسید: <code>غیرفعال‌ها ۳۰</code>",
    "trend": "📈 بنویسید: <code>روند گروه</code>",
    "rules": "📜 بنویسید: <code>قوانین</code>",
    "setrules": "✏️ بنویسید: <code>تنظیم قوانین ۱. احترام ۲. بدون تبلیغ</code>",
    "admins": "👮 بنویسید: <code>لیست مدیران</code>",
    "adminperm": ("🎚 روی پیام آن مدیر <b>ریپلای</b> کنید و بنویسید:\n"
                  "<code>دسترسی مدیر</code>\n"
                  "سپس هر دسترسی را با دکمه‌هایش فعال/غیرفعال کنید.\n"
                  "عزل: <code>تنزل</code> • ارتقا: <code>ارتقا</code>"),
    "stafflist": "🛡 بنویسید: <code>لیست کادر</code>",
    "trustedlist": "⭐️ بنویسید: <code>لیست ویژه‌ها</code>",
    "addtrusted": "➕ روی پیام کاربر ریپلای کنید و بنویسید:\n<code>افزودن ویژه</code>",
    "rmtrusted": "➖ روی پیام کاربر ریپلای کنید و بنویسید:\n<code>عزل ویژه</code>",
    "taglist": "🏷 بنویسید: <code>لیست تگ‌ها</code>",
    "settag": "🏷 روی پیام کاربر ریپلای کنید و بنویسید:\n<code>تگ متن تگ</code>",
    "deltag": "🗑 روی پیام کاربر ریپلای کنید و بنویسید <code>حذف تگ</code>؛ این فقط تگ نمایشی را پاک می‌کند.",
    "tagexclude": ("🚫 <code>حذف از تگ</code> خود مدیر را از «تگ همه» مستثنا می‌کند.\n"
                   "با ریپلای روی پیام عضو، هم مدیر و هم عضو هدف مستثنا می‌شوند.\n"
                   "برای فقط خودتان: <code>حذف از تگ خود</code>."),
    "tagrestore": ("✅ با ریپلای روی پیام عضو بنویسید <code>افزودن به تگ</code>؛ "
                   "برای بازگرداندن خودتان: <code>افزودن به تگ خود</code>."),
    "broadcast": ("📢 در خصوصی ربات بنویسید <code>ارسال همگانی</code> و پیام/متن را تعیین کنید؛ "
                   "مقصدها را انتخاب و مرور کنید. ارسال فقط با تأیید نهایی آغاز می‌شود."),
    "rootgrant": ("🔐 مالک ربات: <code>دسترسی روت @username</code> را در همان گروه اجرا کنید؛ "
                  "کاربر باید همان‌جا مدیر یا مالک باشد. سپس اختیارات را یکی‌یکی تیک بزنید. "
                  "در خصوصی، ابتدا چت فعال را از فهرست انتخاب کنید."),
    "rootrevoke": ("🗑 <code>لغو دسترسی روت @username</code> در گروه فقط همان چت را لغو می‌کند؛ "
                   "در خصوصی همهٔ واگذاری‌های آن کاربر لغو می‌شود."),
    "rootlist": "🔐 مالک بات در خصوصی همهٔ واگذاری‌ها را می‌بیند؛ دارندهٔ «مدیریت روت» در گروه فقط واگذاری‌های همان گروه را می‌بیند.",
    "listfilters": "🚫 بنویسید: <code>لیست فیلتر</code>",
    "listnotes": "📒 بنویسید: <code>لیست یادداشت‌ها</code>",
    "listcommands": "⌨️ بنویسید: <code>لیست دستورات</code>",
    "listmagic": "🎴 بنویسید: <code>لیست استیکرها</code>",
    "pin": "📌 روی پیام ریپلای کنید و بنویسید: <code>پین</code>",
    "unpin": "📍 روی پیام پین‌شده ریپلای کنید و بنویسید: <code>حذف پین</code>",
    "getpin": "📌 بنویسید: <code>پین فعلی</code>",
    "purge100": "🧹 بنویسید: <code>پاکسازی ۱۰۰</code>",
    "setlog": "📜 در گروه/کانال مقصد بنویسید: <code>تنظیم لاگ</code>",
    "listschedule": "⏰ بنویسید: <code>لیست زمان‌بندی</code>",
    "addschedule": "➕ بنویسید: <code>زمان‌بندی روزانه ۰۹:۰۰ متن پیام</code>",
    "backup": "💾 بنویسید: <code>پشتیبان‌گیری</code>",
    "restorehelp": "♻️ فایل پشتیبان را ریپلای کنید و بنویسید: <code>بازیابی</code>",
    "openreports": "📢 بنویسید: <code>گزارش‌ها</code>",
    "joke": "😂 بنویسید: <code>جوک</code>",
    "fal": "🔮 بنویسید: <code>فال حافظ</code>",
    "coin": "🪙 بنویسید: <code>شیر یا خط</code>",
    "dice": "🎲 بنویسید: <code>تاس</code>",
    "slot": "🎰 بنویسید: <code>اسلات</code>",
    "rand": "🔢 بنویسید: <code>عدد تصادفی ۱ ۱۰۰</code>",
    "game": "🎮 بنویسید: <code>بازی</code> سپس عدد حدس خود را ارسال کنید.",
    "currency": "💱 بنویسید: <code>ارز</code> یا <code>قیمت دلار</code>",
    "crypto": "🪙 بنویسید: <code>قیمت بیت‌کوین</code> یا <code>ارز دیجیتال</code>",
    "gold": "🥇 بنویسید: <code>طلا</code> یا <code>سکه</code>",
    "bourse": "📈 بنویسید: <code>بورس</code>",
    "date": "📅 بنویسید: <code>تاریخ</code>",
    "time": "🕒 بنویسید: <code>ساعت</code>",
    "nighthelp": "🌙 تنظیم بازه:\n<code>حالت شب شروع ۲۳:۳۰</code>\n<code>حالت شب پایان ۰۷:۰۰</code>",
    "fedinfo": "🌐 بنویسید: <code>اطلاعات فدراسیون</code>",
    "fbanlist": "🚫 بنویسید: <code>لیست بن فدراسیون</code>",
    "fedcreate": "➕ بنویسید: <code>ساخت فدراسیون نام</code>",
    "fedjoin": "🔗 بنویسید: <code>اتحاد فدراسیون نام</code>",
}


# --------------------------------------------------------------------------- #
# Context building
# --------------------------------------------------------------------------- #
async def _prepare_ctx(session: AsyncSession, bot: Bot, chat_id: int, user_id: int,
                       actor, settings: dict, section: str, **extra) -> dict:
    ctx: dict = {
        "level": actor.role_level if actor else level_of("member"),
        "settings": settings,
        "chat_id": chat_id,
        "chat_title": extra.get("chat_title", ""),
        "chat_type": extra.get("chat_type", "group"),
        "role_label": ROLE_LABELS.get(_role_key(actor), "👤 عضو") if actor else "👤 عضو",
        **extra,
    }
    if section == "ai_chat":
        from ..services.ai_gateway import (
            AIGatewayError,
            AIGatewayNotConfigured,
            list_models,
        )

        try:
            ctx["ai_models"] = await list_models()
            ctx["ai_models_error"] = None
        except AIGatewayNotConfigured:
            ctx["ai_models"] = []
            ctx["ai_models_error"] = "not_configured"
        except AIGatewayError:
            ctx["ai_models"] = []
            ctx["ai_models_error"] = "unavailable"
    if section in {"locks", "lock", "security"}:
        ctx["locks"] = await lock_service.get_locks_map(session, chat_id)
    if section == "lock":
        ctx["lock_key"] = extra.get("lock_key")
        ctx["lock_page"] = extra.get("lock_page", "links")
    if section == "locks":
        ctx["lock_page"] = extra.get("lock_page", "links")
        from ..services import chatlock

        ctx["chat_lock"] = await chatlock.chat_lock_state(bot, chat_id)
    if section == "filters":
        rules = await filter_service.get_rules(session, chat_id)
        notes = await note_service.list_notes(session, chat_id)
        commands = await personal_service.list_commands(session, chat_id)
        ctx["filter_counts"] = {
            "blocklist": sum(1 for r in rules if r.is_blocklist),
            "custom": sum(1 for r in rules if not r.is_blocklist),
            "notes": len(notes),
            "commands": len(commands),
        }
    if section == "members":
        # Telegram is the source of truth for Telegram administrators. Do not
        # report a false zero when the Bot API lookup is unavailable.
        try:
            admins = await bot.get_chat_administrators(chat_id=chat_id)
            admin_count: int | None = len({
                int(item.user.id) for item in admins or []
                if getattr(item, "user", None) is not None
            })
        except Exception as exc:  # noqa: BLE001 - keep the panel usable
            logger.info("could not load administrator count for chat %s: %s",
                        chat_id, exc.__class__.__name__)
            admin_count = None

        staff_rows = await session.execute(
            select(BotRole.user_id).where(BotRole.chat_id == chat_id))
        staff_ids = {int(user_id_) for user_id_ in staff_rows.scalars().all()}
        # Older deployments may have internal roles only on chat_members.
        legacy_staff = await session.execute(
            select(ChatMemberState.user_id).where(
                ChatMemberState.chat_id == chat_id,
                ChatMemberState.bot_role.is_not(None),
                ChatMemberState.bot_role != ""))
        staff_ids.update(int(user_id_) for user_id_ in legacy_staff.scalars().all())

        trusted = await session.execute(
            select(TrustedUser.user_id).where(TrustedUser.chat_id == chat_id))
        tags = await tag_service.tags_of_chat(session, chat_id)
        ctx["member_counts"] = {
            "admins": admin_count,
            "staff": len(staff_ids),
            "trusted": len({int(user_id_) for user_id_ in trusted.scalars().all()}),
            "tags": len(tags),
        }
    if section == "staff":
        result = await session.execute(select(BotRole).where(BotRole.chat_id == chat_id))
        rows = []
        for item in result.scalars().all():
            from ..db.models import User

            user = await session.get(User, item.user_id)
            name = f"{user.first_name or ''} {user.last_name or ''}".strip() if user else str(item.user_id)
            rows.append((item.user_id, item.role, name or str(item.user_id)))
        ctx["staff"] = rows
    if section == "trust":
        result = await session.execute(
            select(TrustedUser).where(TrustedUser.chat_id == chat_id))
        rows = []
        for item in result.scalars().all():
            from ..db.models import User

            user = await session.get(User, item.user_id)
            name = f"{user.first_name or ''} {user.last_name or ''}".strip() if user else str(item.user_id)
            rows.append((item.user_id, name or str(item.user_id)))
        ctx["trusted"] = rows
    if section == "tags":
        rows = await tag_service.tags_of_chat(session, chat_id)
        enriched = []
        from ..db.models import User

        for user_id_, tag in rows:
            user = await session.get(User, user_id_)
            name = f"{user.first_name or ''} {user.last_name or ''}".strip() if user else str(user_id_)
            enriched.append((user_id_, name or str(user_id_), tag))
        ctx["tags"] = enriched
    if section == "schedule":
        result = await session.execute(
            select(ScheduledMessage).where(ScheduledMessage.chat_id == chat_id).limit(15))
        ctx["scheduled"] = [f"#{r.id} - {r.text[:30]}" for r in result.scalars().all()]
    if section == "federation":
        fed_id = settings.get("federation_id")
        if fed_id:
            from ..services import federation as fed_service

            fed = await fed_service.get_fed_by_id(session, int(fed_id))
            if fed:
                ctx["federation"] = {
                    "name": fed.name,
                    "admins": len(await fed_service.fed_admins(session, fed)),
                    "chats": len(await fed_service.fed_chats(session, fed)),
                    "bans": len(await fed_service.fban_list(session, fed)),
                    "mode": "اجرای خودکار" if fed.ban_mode == "enforce" else "اشتراک‌گذاری",
                }
    if section == "reports":
        open_count = await session.scalar(
            select(func.count(Report.id)).where(Report.chat_id == chat_id,
                                                Report.status == "open")) or 0
        total = await session.scalar(
            select(func.count(Report.id)).where(Report.chat_id == chat_id)) or 0
        ctx["report_counts"] = {"open": int(open_count), "total": int(total)}
    if section == "profile":
        from ..db.models import User

        state_result = await session.execute(
            select(ChatMemberState).where(ChatMemberState.chat_id == chat_id,
                                          ChatMemberState.user_id == user_id))
        state = state_result.scalar_one_or_none()
        user = await session.get(User, user_id)
        warns = await session.scalar(
            select(func.count(Warning.id)).where(Warning.chat_id == chat_id,
                                                 Warning.user_id == user_id,
                                                 Warning.active.is_(True))) or 0
        ctx["profile"] = {
            "user_id": user_id,
            "name": f"{user.first_name or ''} {user.last_name or ''}".strip() if user else "—",
            "username": user.username if user else None,
            "role": ROLE_LABELS.get(_role_key(actor), "👤 عضو") if actor else "👤 عضو",
            "tag": state.tag if state else None,
            "messages": int(state.message_count or 0) if state else 0,
            "warns": int(warns),
        }
    return ctx


async def send_panel(bot: Bot, chat_id: int, user_id: int, session: AsyncSession, *,
                     message: Message | None = None, source_chat_id: int | None = None,
                     section: str = "main", **extra) -> None:
    """Send (or edit) the role-aware panel."""
    source_chat_id = source_chat_id or chat_id
    target_chat_id = chat_id
    if (message is not None and message.chat.type == "private") or (
            message is None and chat_id == user_id):
        connected = await connection_service.get_active(session, user_id)
        if connected:
            target_chat_id = connected
        elif section == "main":
            await bot.send_message(
                chat_id=chat_id,
                text="🔗 اتصال فعالی وجود ندارد.\n"
                     "ابتدا در گروه دستور <code>پنل</code> را بزنید، سپس در این چت خصوصی "
                     "دستور <code>اتصال</code> را اجرا کنید.",
                parse_mode=ParseMode.HTML)
            return

    actor = await permissions.build_actor(bot, target_chat_id, user_id, session=session)
    if target_chat_id < 0 and not _can_manage(actor):
        # Ordinary members never see the management panel - they get a small
        # personal card instead.
        await _send_member_panel(bot, chat_id, user_id, session, target_chat_id=target_chat_id)
        return
    settings = await get_settings_cached(session, target_chat_id)
    chat = await session.get(Chat, target_chat_id)
    ctx = await _prepare_ctx(session, bot, target_chat_id, user_id, actor, settings, section,
                             chat_title=(chat.title if chat else ""), chat_type="group", **extra)
    text, keyboard = build_panel(section, ctx)
    # Initial group panels are explicitly exempt from the global 30-second TTL.
    with panel_message_context():
        sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard,
                                      parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    if sent is not None:
        remember_panel_message(sent.chat.id, sent.message_id)
        remember_panel_owner(chat_id, sent.message_id, user_id)


PANEL_TTL = 24 * 60 * 60  # owner binding only; it does not expire the panel message


def remember_panel_owner(chat_id: int, message_id: int, user_id: int) -> None:
    """Bind a panel message to the admin who opened it."""
    cache.panel_owner_cache.set((chat_id, message_id), int(user_id), ttl=PANEL_TTL)


def _panel_owner(chat_id: int, message_id: int) -> int | None:
    owner = cache.panel_owner_cache.get((chat_id, message_id))
    return int(owner) if owner is not None else None


def _role_key(actor) -> str:
    """The actor's role, Telegram admin status included."""
    return (actor.effective_role if hasattr(actor, "effective_role") else actor.bot_role) or "member"


def _can_manage(actor) -> bool:
    """Group management is reserved for Telegram admins and promoted staff."""
    from ..services.roles import Role, level_of

    return actor.is_telegram_admin or actor.role_level >= level_of("helper")


async def _send_member_panel(bot: Bot, chat_id: int, user_id: int, session: AsyncSession,
                             *, target_chat_id: int) -> None:
    """A small personal card for members (no group management at all)."""
    from ..core.normalization import to_persian_digits
    from ..services.chat_state import get_member_state_snapshot

    state = await get_member_state_snapshot(session, target_chat_id, user_id)
    chat = await session.get(Chat, target_chat_id)
    warns = int(state.get("warn_count") or 0)
    tag = state.get("tag") or "—"
    lines = [
        f"🤖 <b>{BOT_NAME}</b>",
        "",
        f"💬 گروه: {chat.title if chat else target_chat_id}",
        f"🆔 شناسه شما: <code>{user_id}</code>",
        f"🎖 سمت شما: {ROLE_LABELS.get('member', 'عضو')}",
        f"🏷 تگ شما: <code>{tag}</code>",
        f"⚠️ اخطارها: {to_persian_digits(str(warns))}",
        "",
        "ℹ️ پنل مدیریت گروه فقط برای مدیران و مالک گروه نمایش داده می‌شود.",
        "برای دیدن دستورات آزاد بنویسید: <code>راهنما</code>",
    ]
    await bot.send_message(chat_id=chat_id, text="\n".join(lines),
                           parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def _refresh(callback: CallbackQuery, session: AsyncSession, bot: Bot, chat_id: int,
                   section: str, **extra) -> None:
    actor = await permissions.build_actor(bot, chat_id, callback.from_user.id, session=session)
    settings = await get_settings_cached(session, chat_id)
    chat = await session.get(Chat, chat_id)
    ctx = await _prepare_ctx(session, bot, chat_id, callback.from_user.id, actor, settings,
                             section, chat_title=(chat.title if chat else ""),
                             chat_type=callback.message.chat.type if callback.message else "group",
                             **extra)
    text, keyboard = build_panel(section, ctx)
    edited = await safe_edit(callback.message, text, reply_markup=keyboard)
    if edited is not None and callback.message is not None:
        remember_panel_owner(chat_id, callback.message.message_id, callback.from_user.id)


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await safe_answer(callback)


@router.callback_query(F.data.startswith("nav:"))
async def on_nav(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    action = parts[1] if len(parts) > 1 else "home"
    if action == "close":
        await safe_answer(callback)
        if callback.message:
            await safe_delete(bot, callback.message.chat.id, callback.message.message_id,
                              context="panel_close")
        return
    if action == "home":
        await safe_answer(callback)
        await _refresh(callback, session, bot, await _panel_chat_id(callback, session), "main")
        return
    await safe_answer(callback)
    await _refresh(callback, session, bot, await _panel_chat_id(callback, session), parts[2] if len(parts) > 2 else "main")


async def _panel_chat_id(callback: CallbackQuery, session: AsyncSession | None = None) -> int:
    """Resolve the group a panel message is controlling.

    A panel opened in a private chat acts on the **connected** group, so the
    chat id must be resolved through the connection table - never taken from
    the private chat itself.
    """
    if not callback.message:
        return callback.from_user.id
    if callback.message.chat.type == "private" and session is not None:
        connected = await connection_service.get_active(session, callback.from_user.id)
        if connected:
            return connected
    return callback.message.chat.id


@router.callback_query(F.data.startswith("panel:"))
async def on_panel(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    section = parts[1] if len(parts) > 1 else "main"
    await safe_answer(callback)
    await _refresh(callback, session, bot, await _panel_chat_id(callback, session), section)


@router.callback_query(F.data.startswith("resetmenu:"))
async def on_reset_menu(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """Offer independent reset choices; there is deliberately no reset-all action."""
    parts = parse_cb(callback.data or "")
    action = parts[1] if len(parts) > 1 else ""
    if action != "open":
        await safe_answer(callback)
        return
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    if callback.message is None:
        await safe_answer(callback, "⛔️ پیام پنل پیدا نشد.", show_alert=True)
        return
    rows = [[primary(f"♻️ {label}", cb("reset", "ask", section))]
            for section, (_panel, label, _description) in RESET_SECTIONS.items()]
    rows.append([primary("⬅️ بازگشت به تنظیمات", cb("panel", "settings"))])
    await safe_answer(callback)
    await safe_edit(
        callback.message,
        "♻️ <b>بازنشانی بخش‌به‌بخش</b>\n\n"
        "یک بخش را انتخاب کنید. هر بخش جداگانه بازنشانی می‌شود و پیش از اجرا تأیید می‌گیرد؛ "
        "هیچ دکمه‌ای برای بازنشانی یکجای کل گروه وجود ندارد.",
        reply_markup=markup(rows), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("reset:"))
async def on_reset_section(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    action = parts[1] if len(parts) > 1 else ""
    section = parts[2] if len(parts) > 2 else ""
    definition = RESET_SECTIONS.get(section)
    if definition is None:
        await safe_answer(callback, "⛔️ بخش بازنشانی معتبر نیست.", show_alert=True)
        return
    panel_section, label, description = definition
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    if callback.message is None:
        await safe_answer(callback, "⛔️ پیام پنل پیدا نشد.", show_alert=True)
        return

    if action == "ask":
        await safe_answer(callback)
        title = (f"⚠️ <b>بازنشانی {label} به پیش‌فرض</b>\n\n"
                 f"{description}\n\n"
                 "فقط همین بخش تغییر می‌کند. آیا ادامه می‌دهید؟")
        await safe_edit(
            callback.message, title,
            reply_markup=markup([
                [danger("♻️ بله، بازنشانی شود", cb("reset", "confirm", section)),
                 primary("✖️ انصراف", cb("reset", "cancel", section))],
            ]), parse_mode=ParseMode.HTML)
        return
    if action == "cancel":
        await safe_answer(callback, "عملیات لغو شد.")
        await _refresh(callback, session, bot, chat_id, panel_section)
        return
    if action != "confirm":
        await safe_answer(callback)
        return

    settings_obj = await get_settings(session, chat_id)
    if section == "welcome":
        from ..services.welcome import DEFAULT_GOODBYE, DEFAULT_WELCOME

        settings_obj.welcome_enabled = True
        settings_obj.welcome_text = DEFAULT_WELCOME
        settings_obj.welcome_media = None
        settings_obj.welcome_buttons = None
        settings_obj.welcome_delete_after = 0
        settings_obj.goodbye_enabled = False
        settings_obj.goodbye_text = DEFAULT_GOODBYE
        settings_obj.goodbye_media = None
        settings_obj.goodbye_buttons = None
        settings_obj.goodbye_delete_after = 0
        for field in ("clean_join", "clean_leave", "clean_pin", "clean_service",
                      "clean_channel_post", "clean_voice_chat"):
            setattr(settings_obj, field, False)
    elif section == "ai":
        settings_obj.ai_enabled = False
        settings_obj.ai_threshold = 0.75
        settings_obj.ai_chat_model = ""
        settings_obj.ai_agent_enabled = False
        await session.execute(delete(GroupMessageMemory).where(
            GroupMessageMemory.chat_id == chat_id))
        from ..services.ai_chat import clear_chat_memory

        clear_chat_memory(chat_id)
    elif section == "locks":
        from ..services.chatlock import apply_chat_lock

        if not await apply_chat_lock(bot, chat_id, "off"):
            await safe_answer(callback,
                              "⚠️ نتوانستم قفل گروه را در تلگرام باز کنم؛ تنظیمات دست‌نخورده ماند.",
                              show_alert=True)
            await _refresh(callback, session, bot, chat_id, panel_section)
            return
        await session.execute(delete(Lock).where(Lock.chat_id == chat_id))
        for field, value in {
            "ban_on_leave": False, "quick_leave_ban": False,
            "quick_leave_seconds": 10, "leave_notify_owner": False,
            "sticky_ban": True, "anonymous_sender_exempt": True,
        }.items():
            setattr(settings_obj, field, value)
    else:
        values = _SECURITY_DEFAULTS if section == "security" else RESET_DEFAULTS.get(section)
        if values is None:
            await safe_answer(callback, "⛔️ برای این بخش پیش‌فرضی تعریف نشده است.", show_alert=True)
            return
        for field, value in values.items():
            setattr(settings_obj, field, value)

    if section in {"security", "raid"}:
        from ..services.antiraid import raid_tracker

        raid_tracker.deactivate(chat_id)
    settings_obj.updated_at = datetime.utcnow()
    invalidate_settings(chat_id)
    await session.flush()
    await safe_answer(callback, "✅ همین بخش به حالت پیش‌فرض برگشت.")
    await _refresh(callback, session, bot, chat_id, panel_section)


@router.callback_query(F.data.startswith("aimodel:"))
async def on_ai_model(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """Live AI model picker; every mutation is restricted to group admins."""
    from ..services.ai_gateway import (
        AIGatewayError,
        AIGatewayNotConfigured,
        list_models,
        model_ref,
    )

    parts = parse_cb(callback.data or "")
    action = parts[1] if len(parts) > 1 else ""
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return

    if action == "page":
        try:
            page = max(0, min(int(parts[2]), 999))
        except (IndexError, TypeError, ValueError):
            page = 0
        await safe_answer(callback)
        await _refresh(callback, session, bot, chat_id, "ai_chat", ai_model_page=page)
        return

    if action == "refresh":
        try:
            await list_models(force_refresh=True)
            answer = "✅ فهرست زندهٔ مدل‌ها تازه شد."
        except AIGatewayNotConfigured:
            answer = "⛔️ AI_CHAT_BASE_URL و AI_CHAT_API_KEY را در Railway تنظیم کنید."
        except AIGatewayError:
            answer = "⚠️ فهرست مدل‌ها دریافت نشد؛ کلید و دسترسی API را بررسی کنید."
        await safe_answer(callback, answer, show_alert=answer.startswith(("⛔️", "⚠️")))
        await _refresh(callback, session, bot, chat_id, "ai_chat", ai_model_page=0)
        return

    if action == "off":
        settings_obj = await get_settings(session, chat_id)
        settings_obj.ai_chat_model = ""
        settings_obj.ai_agent_enabled = False
        settings_obj.updated_at = datetime.utcnow()
        invalidate_settings(chat_id)
        await session.flush()
        await safe_answer(callback, "✅ گفت‌وگوی هوشمند این گروه خاموش شد.")
        await _refresh(callback, session, bot, chat_id, "ai_chat")
        return

    if action == "pick":
        token = parts[2] if len(parts) > 2 else ""
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            await safe_answer(callback, "⛔️ شناسهٔ مدل معتبر نیست؛ فهرست را تازه‌سازی کنید.", show_alert=True)
            return
        try:
            models = await list_models()
        except AIGatewayNotConfigured:
            await safe_answer(callback, "⛔️ AI_CHAT_BASE_URL و AI_CHAT_API_KEY را در Railway تنظیم کنید.", show_alert=True)
            return
        except AIGatewayError:
            await safe_answer(callback, "⚠️ فهرست مدل‌ها در دسترس نیست؛ دوباره تازه‌سازی کنید.", show_alert=True)
            return
        model = next((item for item in models if model_ref(item.id) == token), None)
        if model is None:
            await safe_answer(callback, "⚠️ این مدل دیگر در فهرست زنده نیست؛ فهرست را تازه‌سازی کنید.", show_alert=True)
            return
        settings_obj = await get_settings(session, chat_id)
        settings_obj.ai_chat_model = model.id
        settings_obj.ai_agent_enabled = True
        settings_obj.updated_at = datetime.utcnow()
        invalidate_settings(chat_id)
        await session.flush()
        await safe_answer(callback, "✅ مدل برای این گروه انتخاب شد.")
        await _refresh(callback, session, bot, chat_id, "ai_chat")
        return

    await safe_answer(callback)


@router.callback_query(F.data.startswith("aimemory:"))
async def on_ai_memory(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    action = parts[1] if len(parts) > 1 else ""
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    if callback.message is None:
        await safe_answer(callback, "⛔️ پیام پنل پیدا نشد.", show_alert=True)
        return

    if action == "ask_clear":
        await safe_answer(callback)
        await safe_edit(
            callback.message,
            "⚠️ <b>پاک‌کردن حافظهٔ گروه</b>\n\n"
            "تمام پیام‌های ذخیره‌شدهٔ این گروه برای هوش مصنوعی پاک می‌شوند. "
            "این کار برگشت‌پذیر نیست. ادامه می‌دهید؟",
            reply_markup=markup([
                [danger("🧹 بله، پاک شود", cb("aimemory", "confirm_clear")),
                 primary("✖️ انصراف", cb("aimemory", "cancel_clear"))],
            ]))
        return
    if action == "cancel_clear":
        await safe_answer(callback, "عملیات لغو شد.")
        await _refresh(callback, session, bot, chat_id, "ai_chat")
        return
    if action == "confirm_clear":
        from ..services.ai_memory import clear_group_memory
        from ..services.ai_chat import clear_chat_memory

        count = await clear_group_memory(session, chat_id)
        clear_chat_memory(chat_id)
        await safe_answer(callback, f"✅ {to_persian_digits(str(count))} پیام از حافظه پاک شد.")
        await _refresh(callback, session, bot, chat_id, "ai_chat")
        return
    await safe_answer(callback)


@router.callback_query(F.data.startswith("lockpage:"))
async def on_lock_page(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    page = parts[1] if len(parts) > 1 else "links"
    await safe_answer(callback)
    await _refresh(callback, session, bot, await _panel_chat_id(callback, session), "locks", lock_page=page)


@router.callback_query(F.data.startswith("lock:"))
async def on_lock(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    if len(parts) < 3:
        await safe_answer(callback)
        return
    action, key = parts[1], parts[2]
    page = parts[3] if len(parts) > 3 else "links"
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    await safe_answer(callback)
    if action == "toggle":
        await lock_service.toggle_lock(session, chat_id, key, updated_by=callback.from_user.id)
        if key in {"profanity", "porn"}:
            settings_obj = await get_settings(session, chat_id)
            locks = await lock_service.get_locks_map(session, chat_id)
            enabled = bool(locks.get(key) and locks[key].enabled)
            if key == "profanity":
                settings_obj.anti_profanity_enabled = enabled
            else:
                settings_obj.anti_porn_enabled = enabled
            invalidate_settings(chat_id)
    await _refresh(callback, session, bot, chat_id, "locks", lock_page=page)


@router.callback_query(F.data.startswith("lockact:"))
async def on_lock_action(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    # lockact:<key>:<action>:<page>
    if len(parts) < 3:
        await safe_answer(callback)
        return
    key, action = parts[1], parts[2]
    page = parts[3] if len(parts) > 3 else "links"
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    await safe_answer(callback)
    locks = await lock_service.get_locks_map(session, chat_id)
    state = locks.get(key)
    await lock_service.set_lock(session, chat_id, key, True, action=action,
                                duration=(state.duration if state else None),
                                updated_by=callback.from_user.id)
    await _refresh(callback, session, bot, chat_id, "locks", lock_page=page)


@router.callback_query(F.data.startswith("set:"))
async def on_set(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    # set:<field>:<value>[:<panel>]
    if len(parts) < 3:
        await safe_answer(callback)
        return
    field, raw_value = parts[1], parts[2]
    panel = parts[3] if len(parts) > 3 else "settings"
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    if field == "report_delete_after":
        await safe_answer(callback, "⏱ پیام‌های عادی گروه پس از ۳۰ ثانیه پاک می‌شوند؛ پنل مدیریتی باقی می‌ماند.", show_alert=True)
        return
    settings_obj = await get_settings(session, chat_id)
    if not hasattr(settings_obj, field):
        await safe_answer(callback, "⛔️ تنظیم نامعتبر است.", show_alert=True)
        return
    current = getattr(settings_obj, field)
    if isinstance(current, bool):
        value = raw_value.lower() in {"1", "true", "yes", "on"}
    elif isinstance(current, int):
        try:
            value = int(raw_value)
        except ValueError:
            await safe_answer(callback, "⛔️ مقدار نامعتبر.", show_alert=True)
            return
    else:
        value = raw_value
    setattr(settings_obj, field, value)
    settings_obj.updated_at = datetime.utcnow()
    invalidate_settings(chat_id)
    await session.flush()
    await safe_answer(callback, "✅ ذخیره شد.")
    await _refresh(callback, session, bot, chat_id, panel)


@router.callback_query(F.data.startswith("num:"))
async def on_number(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    # num:<field>:<delta>:<panel>:<min>:<max>
    if len(parts) < 4:
        await safe_answer(callback)
        return
    field, delta = parts[1], int(parts[2])
    panel = parts[3]
    minimum = int(parts[4]) if len(parts) > 4 else 0
    maximum = int(parts[5]) if len(parts) > 5 else 9999
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    if field == "report_delete_after":
        await safe_answer(callback, "⏱ پیام‌های عادی گروه پس از ۳۰ ثانیه پاک می‌شوند؛ پنل مدیریتی باقی می‌ماند.", show_alert=True)
        return
    settings_obj = await get_settings(session, chat_id)
    current = int(getattr(settings_obj, field, 0) or 0)
    value = max(minimum, min(maximum, current + delta))
    setattr(settings_obj, field, value)
    invalidate_settings(chat_id)
    await session.flush()
    await safe_answer(callback, f"✅ {to_persian_digits(str(value))}")
    await _refresh(callback, session, bot, chat_id, panel)


@router.callback_query(F.data.startswith("dis:"))
async def on_disabled_toggle(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    key = parts[1] if len(parts) > 1 else ""
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    await safe_answer(callback)
    if await disabled_service.is_disabled(session, chat_id, key):
        await disabled_service.enable(session, chat_id, key)
    else:
        await disabled_service.disable(session, chat_id, key)
    await _refresh(callback, session, bot, chat_id, "disabled")


@router.callback_query(F.data.startswith("conf:"))
async def on_confirm(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else "all"
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    await safe_answer(callback)
    keyboard = confirm(cb("do", action, arg), cb("nav", "home"),
                       target="main")
    await safe_edit(callback.message, f"{confirm_text(action)}\n\n⚠️ این عملیات قابل بازگشت نیست.",
                    reply_markup=keyboard)


@router.callback_query(F.data.startswith("do:"))
async def on_do(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    from ..services import backup as backup_service

    parts = parse_cb(callback.data or "")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else "all"
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    await safe_answer(callback)
    done = "✅ انجام شد."

    if action == "clearwarns":
        from ..services import moderation as mod

        result = await session.execute(
            select(Warning).where(Warning.chat_id == chat_id, Warning.active.is_(True)))
        count = 0
        for row in result.scalars().all():
            row.active = False
            row.removed_at = datetime.utcnow()
            row.removed_by = callback.from_user.id
            count += 1
        await session.execute(
            select(ChatMemberState).where(ChatMemberState.chat_id == chat_id))
        await session.flush()
        from ..services.chat_state import get_member_state

        states = await session.execute(
            select(ChatMemberState).where(ChatMemberState.chat_id == chat_id))
        for state in states.scalars().all():
            state.warn_count = 0
        await session.flush()
        done = f"🧹 {to_persian_digits(str(count))} اخطار پاک شد."
    elif action == "clearfilters":
        count = await filter_service.clear_rules(session, chat_id, blocklist=True, custom=False)
        done = f"🧹 {to_persian_digits(str(count))} فیلتر حذف شد."
    elif action == "clearnotes":
        count = await note_service.clear_notes(session, chat_id)
        done = f"🧹 {to_persian_digits(str(count))} یادداشت حذف شد."
    elif action == "clearlocks":
        count = await lock_service.clear_locks(session, chat_id)
        done = f"🧹 {to_persian_digits(str(count))} قفل غیرفعال شد."
    elif action == "unpinall":
        from ..services import pins as pin_service

        ok = await pin_service.unpin_all(bot, chat_id)
        done = "🧹 همه پین‌ها حذف شدند." if ok else "⚠️ عملیات انجام نشد."
    elif action == "delrules":
        settings_obj = await get_settings(session, chat_id)
        settings_obj.rules_text = ""
        invalidate_settings(chat_id)
        done = "🗑 قوانین حذف شد."
    elif action == "clearschedule":
        result = await session.execute(
            select(ScheduledMessage).where(ScheduledMessage.chat_id == chat_id))
        count = 0
        for row in result.scalars().all():
            await session.delete(row)
            count += 1
        await session.flush()
        done = f"🗑 {to_persian_digits(str(count))} زمان‌بندی حذف شد."
    elif action == "fedleave":
        from ..services import federation as fed_service

        await fed_service.leave_fed(session, chat_id)
        done = "🚪 گروه از فدراسیون خارج شد."
    elif action == "ban":
        from ..services import moderation as mod
        from ..services.targeting import html_user

        user_id = int(arg) if arg.isdigit() else 0
        if user_id:
            actor = await permissions.build_actor(bot, chat_id, callback.from_user.id,
                                                  session=session)
            await mod.ban_user(bot, session, chat_id=chat_id, actor=actor, target_id=user_id,
                               target_name=str(user_id), reason="تأیید از پنل")
            done = "🔨 کاربر بن شد."
    else:
        done = "ℹ️ عملیات نامشخص است."

    await safe_edit(callback.message, done, reply_markup=markup([row(primary("🏠 خانه", cb("nav", "home")))]))


@router.callback_query(F.data.startswith("w:"))
async def on_warn_action(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    if len(parts) < 3:
        await safe_answer(callback)
        return
    action, raw_user = parts[1], parts[2]
    user_id = int(raw_user) if raw_user.isdigit() else 0
    try:
        history_page = max(0, int(parts[3])) if len(parts) > 3 else 0
    except ValueError:
        history_page = 0
    chat_id = await _panel_chat_id(callback, session)
    actor = await permissions.build_actor(bot, chat_id, callback.from_user.id, session=session)
    if not (actor.can_ban() or actor.can_restrict() or actor.has_role("moderator")):
        await safe_answer(callback, "⛔️ شما اجازه این کار را ندارید.", show_alert=True)
        return
    if not user_id:
        await safe_answer(callback)
        return
    await safe_answer(callback, "⏳ ...")
    from ..services import moderation as mod

    history_markup = None
    history_mode = False
    if action == "warn":
        text, _ = await mod.warn_user(bot, session, chat_id=chat_id, actor=actor,
                                      target_id=user_id, target_name=str(user_id),
                                      reason="اخطار از پنل")
    elif action == "unwarn":
        text = await mod.unwarn_user(bot, session, chat_id=chat_id, actor=actor,
                                     target_id=user_id, target_name=str(user_id))
    elif action == "reset":
        count = await mod.reset_warnings(session, chat_id, user_id,
                                         by_id=callback.from_user.id, bot=bot, actor=actor)
        text = count if isinstance(count, str) else f"♻️ {to_persian_digits(str(count))} اخطار پاک شد."
    elif action == "history":
        page_size = 10
        total_count = await mod.user_history_count(session, chat_id, user_id)
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        history_page = min(history_page, total_pages - 1)
        actions = await mod.user_history(session, chat_id, user_id,
                                         limit=page_size, offset=history_page * page_size)
        names = await mod.history_user_names(session, actions)
        title = f"📜 تاریخچه {names.get(user_id, str(user_id))}"
        text = mod.history_text(actions, title=title, user_id=user_id, user_names=names,
                                total_count=total_count, page=history_page,
                                page_size=page_size)
        nav = []
        if history_page > 0:
            nav.append(primary("⬅️ قبلی", cb("w", "history", user_id, history_page - 1)))
        if history_page + 1 < total_pages:
            nav.append(primary("بعدی ➡️", cb("w", "history", user_id, history_page + 1)))
        history_markup = markup([nav]) if nav else None
        history_mode = True
    else:
        text = "ℹ️ عملیات نامشخص است."
    if history_mode:
        await safe_edit(callback.message, text, parse_mode=ParseMode.HTML,
                        reply_markup=history_markup)
    else:
        await safe_edit(callback.message, text)


@router.callback_query(F.data.startswith("locknum:"))
async def on_lock_number(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """Change the threshold of a lock (e.g. the 1000 character limit)."""
    parts = parse_cb(callback.data or "")
    # locknum:<key>:<field>:<delta>[:<page>]
    if len(parts) < 4:
        await safe_answer(callback)
        return
    key, field, raw_delta = parts[1], parts[2], parts[3]
    page = parts[4] if len(parts) > 4 else "links"
    try:
        delta = int(raw_delta)
    except ValueError:
        await safe_answer(callback)
        return
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    spec = lock_service.LOCK_REGISTRY.get(key)
    if spec is None or not spec.threshold:
        await safe_answer(callback)
        return
    locks = await lock_service.get_locks_map(session, chat_id)
    state = locks.get(key)
    current = int((state.extra or {}).get(field, spec.default_threshold)) if state \
        else spec.default_threshold
    value = max(1, min(100000, current + delta))
    await lock_service.set_lock(session, chat_id, key,
                                bool(state.enabled) if state else True,
                                action=(state.action if state else None),
                                extra={field: value}, updated_by=callback.from_user.id)
    await safe_answer(callback, f"✅ {to_persian_digits(str(value))}")
    await _refresh(callback, session, bot, chat_id, "lock", lock_key=key, lock_page=page)


@router.callback_query(F.data.startswith("lg:"))
async def on_leave_guard(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """Disable a «قفل خروج» rule straight from the ban notice."""
    from ..services.chat_state import get_settings, invalidate_settings

    parts = parse_cb(callback.data or "")
    if len(parts) < 3:
        await safe_answer(callback)
        return
    action, field = parts[1], parts[2]
    chat_id = await _panel_chat_id(callback, session)
    if not await _ensure_admin(callback, bot, chat_id, session):
        return
    if action != "off" or field not in {"ban_on_leave", "quick_leave_ban"}:
        await safe_answer(callback)
        return
    settings_obj = await get_settings(session, chat_id)
    setattr(settings_obj, field, False)
    invalidate_settings(chat_id)
    await session.flush()
    await safe_answer(callback, "⛔️ قانون خاموش شد.")
    if callback.message:
        label = "بن هنگام خروج" if field == "ban_on_leave" else "بن خروج سریع"
        await safe_edit(callback.message,
                        f"⛔️ «{label}» خاموش شد.\n"
                        f"از این لحظه کسی به‌خاطر خروج بن نمی‌شود.\n\n"
                        f"برای روشن کردن دوباره: <code>قفل خروج</code>")


@router.callback_query(F.data.startswith("undo:"))
async def on_undo(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """Reverse the moderation action the button was attached to."""
    from ..core.telegram_extra import call_api_raw
    from ..keyboards.menus import RIGHT_PRESETS
    from ..services import moderation as mod
    from ..services import tags as tag_service
    from ..services.chat_state import invalidate_member

    parts = parse_cb(callback.data or "")
    if len(parts) < 3:
        await safe_answer(callback)
        return
    action, raw_user = parts[1], parts[2]
    user_id = int(raw_user) if raw_user.isdigit() else 0
    chat_id = await _panel_chat_id(callback, session)
    actor = await permissions.build_actor(bot, chat_id, callback.from_user.id, session=session)
    if not user_id:
        await safe_answer(callback)
        return

    if action == "unmute":
        if not actor.can_restrict():
            await safe_answer(callback, "⛔️ شما اجازه «محدود کردن اعضا» را ندارید.",
                              show_alert=True)
            return
        text = await mod.unmute_user(bot, session, chat_id=chat_id, actor=actor,
                                     target_id=user_id, target_name=str(user_id),
                                     reason="لغو از دکمه")
    elif action == "unban":
        if not actor.can_ban():
            await safe_answer(callback, "⛔️ شما اجازه «بن/رفع بن» را ندارید.", show_alert=True)
            return
        text = await mod.unban_user(bot, session, chat_id=chat_id, actor=actor,
                                    target_id=user_id, target_name=str(user_id),
                                    reason="لغو از دکمه")
    elif action == "unwarn":
        if not (actor.can_ban() or actor.can_restrict() or actor.has_role("moderator")):
            await safe_answer(callback, "⛔️ شما اجازه این کار را ندارید.", show_alert=True)
            return
        text = await mod.unwarn_user(bot, session, chat_id=chat_id, actor=actor,
                                     target_id=user_id, target_name=str(user_id))
    elif action == "demote":
        if not actor.can_promote():
            await safe_answer(callback, "⛔️ فقط مدیران گروه می‌توانند مدیر عزل کنند.",
                              show_alert=True)
            return
        if not await permissions.bot_has_right(bot, chat_id, "can_promote_members"):
            await safe_answer(callback, "⚠️ ربات دسترسی «افزودن مدیر جدید» را ندارد.",
                              show_alert=True)
            return
        payload = {"chat_id": chat_id, "user_id": user_id, "can_manage_chat": True,
                   **RIGHT_PRESETS["none"]}
        if not await call_api_raw(bot, "promoteChatMember", payload):
            await safe_answer(callback, "⚠️ تلگرام این تغییر را نپذیرفت.", show_alert=True)
            return
        invalidate_member(chat_id, user_id)
        text = "↩️ مدیر عزل شد و همهٔ دسترسی‌های او گرفته شد."
    elif action == "deltag":
        if not actor.can_manage_settings():
            await safe_answer(callback, "⛔️ فقط مدیران گروه می‌توانند تگ را حذف کنند.",
                              show_alert=True)
            return
        await tag_service.set_tag(session, chat_id, user_id, None)
        await tag_service.apply_member_tag(bot, chat_id, user_id, None)
        text = "↩️ تگ کاربر حذف شد."
    else:
        await safe_answer(callback)
        return

    if text.startswith(("⛔️", "⚠️", "ℹ️")):
        await safe_answer(callback, text, show_alert=True)
        return
    await safe_answer(callback)
    if callback.message:
        await safe_edit(callback.message, f"↩️ <b>لغو شد</b>\n\n{text}")


@router.callback_query(F.data.startswith("cl:"))
async def on_chat_lock(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """قفل گروه / باز کردن گروه straight from the locks panel."""
    from ..services import chatlock

    parts = parse_cb(callback.data or "")
    mode = parts[1] if len(parts) > 1 else ""
    chat_id = await _panel_chat_id(callback, session)
    actor = await permissions.build_actor(bot, chat_id, callback.from_user.id, session=session)
    if not actor.can_restrict():
        await safe_answer(callback, "⛔️ شما اجازه «محدود کردن اعضا» را ندارید.", show_alert=True)
        return
    if mode not in chatlock.MODES:
        await safe_answer(callback)
        return
    if not await permissions.bot_has_right(bot, chat_id, "can_restrict_members"):
        await safe_answer(callback, "⚠️ ربات دسترسی «محدود کردن اعضا» را ندارد.", show_alert=True)
        return
    if not await chatlock.apply_chat_lock(bot, chat_id, mode):
        await safe_answer(callback, "⚠️ تلگرام این تغییر را نپذیرفت.", show_alert=True)
        return
    await safe_answer(callback, "✅ اعمال شد.")
    if callback.message:
        state = await chatlock.chat_lock_state(bot, chat_id)
        await safe_edit(callback.message,
                        f"{chatlock.MODE_MESSAGES[mode]}\n\n"
                        f"وضعیت کنونی: {chatlock.MODE_LABELS[state]}")


@router.callback_query(F.data.startswith("ap:"))
async def on_admin_rights(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """Toggle the Telegram administrator rights of one admin, live."""
    from ..core.telegram_extra import call_api_raw
    from ..keyboards.menus import (ADMIN_RIGHTS, RIGHT_PRESETS, admin_rights_keyboard,
                                   admin_rights_lines)
    from ..services.chat_state import invalidate_member

    parts = parse_cb(callback.data or "")
    if len(parts) < 3:
        await safe_answer(callback)
        return
    action, raw_user = parts[1], parts[2]
    key = parts[3] if len(parts) > 3 else ""
    user_id = int(raw_user) if raw_user.isdigit() else 0
    chat_id = await _panel_chat_id(callback, session)
    actor = await permissions.build_actor(bot, chat_id, callback.from_user.id, session=session)
    if not actor.can_promote():
        await safe_answer(callback, "⛔️ فقط مدیران گروه می‌توانند دسترسی مدیران را تغییر دهند.",
                          show_alert=True)
        return
    if not user_id:
        await safe_answer(callback)
        return
    if action == "x":
        await safe_answer(callback)
        if callback.message:
            await safe_edit(callback.message, "✅ بسته شد.")
        return
    if not await permissions.bot_has_right(bot, chat_id, "can_promote_members"):
        await safe_answer(callback, "⚠️ ربات دسترسی «افزودن مدیر جدید» را ندارد.",
                          show_alert=True)
        return

    member = await permissions.fetch_chat_member(bot, chat_id, user_id, refresh=True)
    if member is None:
        await safe_answer(callback, "⚠️ وضعیت این کاربر قابل تشخیص نیست.", show_alert=True)
        return
    if isinstance(member, ChatMemberOwner):
        await safe_answer(callback, "👑 دسترسی‌های مالک گروه قابل ویرایش نیست.", show_alert=True)
        return
    if not isinstance(member, ChatMemberAdministrator):
        await safe_answer(callback, "ℹ️ این کاربر دیگر مدیر گروه نیست.", show_alert=True)
        return
    if action != "r" and not member.can_be_edited:
        await safe_answer(callback,
                          "⚠️ این مدیر توسط ربات منصوب نشده است؛ ابتدا با دستور "
                          "«ارتقا» او را دوباره منصوب کنید.", show_alert=True)
        return

    name = member.user.full_name if member.user else str(user_id)
    rights = {flag: bool(getattr(member, flag, False)) for flag, _label, _d in ADMIN_RIGHTS}
    if action == "t" and key in rights:
        rights[key] = not rights[key]
    elif action in RIGHT_PRESETS:
        rights = dict(RIGHT_PRESETS[action])
    elif action == "demote":
        rights = dict(RIGHT_PRESETS["none"])
    elif action not in {"r", "t"}:
        await safe_answer(callback)
        return

    if action != "r":
        # Every flag is transmitted explicitly - False must reach Telegram.
        payload = {"chat_id": chat_id, "user_id": user_id, "can_manage_chat": True, **rights}
        ok = await call_api_raw(bot, "promoteChatMember", payload)
        invalidate_member(chat_id, user_id)
        if not ok:
            await safe_answer(callback, "⚠️ تلگرام این تغییر را نپذیرفت. "
                                        "دسترسی ربات را بررسی کنید.", show_alert=True)
            return
        await safe_answer(callback, "✅ اعمال شد.")
        member = await permissions.fetch_chat_member(bot, chat_id, user_id, refresh=True)
        if not isinstance(member, ChatMemberAdministrator):
            if callback.message:
                await safe_edit(callback.message,
                                f"⬇️ {name} از مدیریت گروه برکنار شد.")
            return
        rights = {flag: bool(getattr(member, flag, False)) for flag, _label, _d in ADMIN_RIGHTS}
    else:
        await safe_answer(callback, "🔄 به‌روز شد.")

    if callback.message:
        await safe_edit(callback.message,
                        "\n".join(admin_rights_lines(name, user_id, rights)),
                        reply_markup=admin_rights_keyboard(user_id, rights))


@router.callback_query(F.data.startswith("rep:"))
async def on_report_action(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    if len(parts) < 3:
        await safe_answer(callback)
        return
    report_id = int(parts[1]) if parts[1].isdigit() else 0
    action = parts[2]
    report = await session.get(Report, report_id)
    if report is None:
        await safe_answer(callback, "ℹ️ این گزارش دیگر وجود ندارد.", show_alert=True)
        return
    chat_id = report.chat_id
    actor = await permissions.build_actor(bot, chat_id, callback.from_user.id, session=session)
    await safe_answer(callback)

    if action == "close":
        await report_service.resolve_report(session, report_id, "closed",
                                            handled_by=callback.from_user.id)
        if callback.message:
            await safe_edit(callback.message, "❌ گزارش بسته شد.")
        return
    if action == "ignore":
        await report_service.resolve_report(session, report_id, "ignored",
                                            handled_by=callback.from_user.id)
        if callback.message:
            await safe_edit(callback.message, "✅ گزارش بدون اقدام بسته شد.")
        return

    if action == "delete":
        if not actor.can_delete():
            await safe_answer(callback, "⛔️ شما اجازه حذف پیام را ندارید.", show_alert=True)
            return
        from ..core.errors import safe_delete

        await safe_delete(bot, chat_id, report.message_id, context="report_delete")
        await report_service.resolve_report(session, report_id, "deleted",
                                            handled_by=callback.from_user.id)
        if callback.message:
            await safe_edit(callback.message, "🗑 پیام حذف شد.")
        return

    from ..services import moderation as mod

    if action == "ban" and not actor.can_ban():
        await safe_answer(callback, "⛔️ شما اجازه بن کردن را ندارید.", show_alert=True)
        return
    if action in {"mute", "warn"} and not actor.can_restrict():
        await safe_answer(callback, "⛔️ شما اجازه محدود کردن اعضا را ندارید.", show_alert=True)
        return
    if not report.target_id:
        await safe_answer(callback, "ℹ️ هدف این گزارش مشخص نیست.", show_alert=True)
        return

    if action == "ban":
        text = await mod.ban_user(bot, session, chat_id=chat_id, actor=actor,
                                  target_id=report.target_id, target_name=str(report.target_id),
                                  reason=f"گزارش #{report_id}")
    elif action == "mute":
        text = await mod.mute_user(bot, session, chat_id=chat_id, actor=actor,
                                   target_id=report.target_id, target_name=str(report.target_id),
                                   reason=f"گزارش #{report_id}")
    else:
        text, _ = await mod.warn_user(bot, session, chat_id=chat_id, actor=actor,
                                      target_id=report.target_id,
                                      target_name=str(report.target_id),
                                      reason=f"گزارش #{report_id}")
    await report_service.resolve_report(session, report_id, f"action:{action}",
                                        handled_by=callback.from_user.id)
    if callback.message:
        await safe_edit(callback.message, text)


@router.callback_query(F.data.startswith("conn:"))
async def on_connect(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    chat_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if not chat_id:
        await safe_answer(callback)
        return
    if not await connection_service.is_authorized(bot, session, callback.from_user.id, chat_id):
        await safe_answer(callback, "⛔️ شما در این گروه مدیر نیستید.", show_alert=True)
        return
    await connection_service.set_active(session, callback.from_user.id, chat_id)
    await safe_answer(callback, "🔗 اتصال برقرار شد.")
    await send_panel(bot, callback.from_user.id, callback.from_user.id, session,
                     message=callback.message)


@router.callback_query(F.data.startswith("help:"))
async def on_help(callback: CallbackQuery) -> None:
    parts = parse_cb(callback.data or "")
    topic = parts[1] if len(parts) > 1 else ""
    await safe_answer(callback)
    if callback.message:
        await safe_edit(callback.message, help_text(topic),
                        reply_markup=markup([row(primary("⬅️ بازگشت", cb("panel", "help"))),
                                             row(primary("🏠 خانه", cb("nav", "home")))]))


@router.callback_query(F.data.startswith("cmd:"))
async def on_command_hint(callback: CallbackQuery) -> None:
    parts = parse_cb(callback.data or "")
    name = parts[1] if len(parts) > 1 else ""
    hint = COMMAND_HINTS.get(name, "ℹ️ این دستور از طریق پنل در دسترس نیست.")
    await safe_answer(callback, show_alert=False)
    if callback.message:
        await safe_edit(callback.message, hint,
                        reply_markup=markup([row(primary("⬅️ بازگشت", cb("nav", "home")))]))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _ensure_admin(callback: CallbackQuery, bot: Bot, chat_id: int,
                        session: AsyncSession) -> bool:
    actor = await permissions.build_actor(bot, chat_id, callback.from_user.id, session=session)
    if actor.can_manage_settings():
        return True
    await safe_answer(callback, "⛔️ فقط مدیران گروه می‌توانند تنظیمات را تغییر دهند.",
                      show_alert=True)
    return False


@router.callback_query(F.data.startswith("bc:"))
async def on_owner_broadcast(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """Owner broadcast picker, preview, confirmation, and delivery."""
    import asyncio

    from ..services import owner_access, owner_broadcast

    parts = parse_cb(callback.data or "")
    action = parts[1] if len(parts) > 1 else ""
    user_id = int(callback.from_user.id)
    is_owner = owner_broadcast.is_bot_owner(user_id)
    if action == "noop":
        await safe_answer(callback)
        return

    draft_id = 0
    if len(parts) > 2 and parts[2].lstrip("-").isdigit():
        draft_id = int(parts[2])

    if action == "switch":
        if not is_owner:
            await safe_answer(callback, "⛔️ فقط مالک ربات می‌تواند ارسال همگانی را روشن یا خاموش کند.",
                              show_alert=True)
            return
        state = await owner_broadcast.get_bot_settings(session)
        state.broadcast_enabled = not bool(state.broadcast_enabled)
        state.updated_by = user_id
        await session.flush()
        if draft_id:
            draft = await owner_broadcast.get_draft(session, draft_id, user_id)
            if draft is not None and callback.message is not None:
                if state.broadcast_enabled:
                    draft.stage = "select"
                    draft.page = 0
                draft.updated_at = datetime.utcnow()
                chats = await owner_broadcast.effective_chats(
                    session, user_id,
                    message_chat_id=callback.message.chat.id,
                    message_chat_type=callback.message.chat.type)
                await safe_edit(
                    callback.message,
                    owner_broadcast._selection_text(draft, chats, bool(state.broadcast_enabled),
                                                    delegated=False),
                    reply_markup=owner_broadcast.selection_keyboard(
                        draft, chats, bool(state.broadcast_enabled), delegated=False,
                        report_delete_after=int(state.broadcast_report_delete_after or 0)),
                    parse_mode=ParseMode.HTML)
            elif callback.message is not None:
                await safe_edit(
                    callback.message,
                    "📢 وضعیت ارسال همگانی تغییر کرد: "
                    + ("🟢 روشن" if state.broadcast_enabled else "🔴 خاموش")
                    + "\nبرای آماده‌سازی پیام، دوباره فرمان ارسال همگانی را بفرستید.",
                    reply_markup=owner_broadcast.switch_keyboard(
                        bool(state.broadcast_enabled),
                        int(state.broadcast_report_delete_after or 0)),
                    parse_mode=ParseMode.HTML)
        elif callback.message is not None:
            await safe_edit(
                callback.message,
                "📢 وضعیت ارسال همگانی: "
                + ("🟢 روشن" if state.broadcast_enabled else "🔴 خاموش")
                + "\nبرای ادامه، فرمان ارسال همگانی را دوباره بفرستید.",
                reply_markup=owner_broadcast.switch_keyboard(
                    bool(state.broadcast_enabled),
                    int(state.broadcast_report_delete_after or 0)),
                parse_mode=ParseMode.HTML)
        await safe_answer(callback, "✅ وضعیت ذخیره شد.")
        return

    if action == "delay":
        if not is_owner:
            await safe_answer(callback, "⛔️ فقط مالک ربات می‌تواند زمان حذف نتیجهٔ خصوصی را تغییر دهد.",
                              show_alert=True)
            return
        if len(parts) < 4 or not parts[3].lstrip("-").isdigit():
            await safe_answer(callback, "⛔️ مقدار زمان نامعتبر است.", show_alert=True)
            return
        delta = int(parts[3])
        if delta not in {-1, 1}:
            await safe_answer(callback, "⛔️ مقدار تغییر نامعتبر است.", show_alert=True)
            return
        state = await owner_broadcast.get_bot_settings(session)
        current_delay = getattr(state, "broadcast_report_delete_after", 12)
        current_delay = 12 if current_delay is None else int(current_delay)
        state.broadcast_report_delete_after = max(0, min(600, current_delay + delta))
        state.updated_by = user_id
        await session.flush()
        value = int(state.broadcast_report_delete_after)
        value_text = "خاموش" if value == 0 else f"{to_persian_digits(str(value))} ثانیه"
        await safe_answer(callback, f"✅ زمان حذف نتیجهٔ خصوصی: {value_text}")
        if callback.message is not None:
            if draft_id:
                draft = await owner_broadcast.get_draft(session, draft_id, user_id)
                if draft is not None:
                    chats = await owner_broadcast.effective_chats(
                        session, user_id,
                        message_chat_id=callback.message.chat.id,
                        message_chat_type=callback.message.chat.type)
                    if draft.stage == "review":
                        await safe_edit(
                            callback.message,
                            owner_broadcast.review_text(draft, chats, delegated=False),
                            reply_markup=owner_broadcast.review_keyboard(
                                draft, bool(state.broadcast_enabled), delegated=False,
                                report_delete_after=value),
                            parse_mode=ParseMode.HTML)
                    else:
                        await safe_edit(
                            callback.message,
                            owner_broadcast._selection_text(
                                draft, chats, bool(state.broadcast_enabled), delegated=False),
                            reply_markup=owner_broadcast.selection_keyboard(
                                draft, chats, bool(state.broadcast_enabled), delegated=False,
                                report_delete_after=value),
                            parse_mode=ParseMode.HTML)
            else:
                await safe_edit_markup(
                    callback.message,
                    owner_broadcast.switch_keyboard(bool(state.broadcast_enabled), value))
        return

    draft = await owner_broadcast.get_draft(session, draft_id, user_id) if draft_id else None
    if draft is None:
        await safe_answer(callback, "ℹ️ این پیش‌نویس منقضی شده یا دیگر معتبر نیست.", show_alert=True)
        return
    delegated = not is_owner
    chats = await owner_broadcast.effective_chats(
        session, user_id,
        message_chat_id=callback.message.chat.id if callback.message else 0,
        message_chat_type=callback.message.chat.type if callback.message else "private")
    if delegated and not chats and action != "cancel":
        await safe_answer(callback, "⛔️ دسترسی روت فعالی برای هیچ چتی ندارید.", show_alert=True)
        return
    state = await owner_broadcast.get_bot_settings(session)

    if action == "cancel":
        await owner_broadcast.cancel_draft(session, draft)
        await safe_answer(callback, "پیش‌نویس لغو شد.")
        if callback.message is not None:
            await safe_edit(callback.message, "❌ پیش‌نویس ارسال همگانی لغو شد.")
        return

    if not state.broadcast_enabled:
        await safe_answer(callback, "🔴 ارسال همگانی خاموش است؛ فقط مالک ربات می‌تواند آن را روشن کند.",
                          show_alert=True)
        if callback.message is not None:
            await safe_edit(
                callback.message,
                owner_broadcast._selection_text(draft, chats, False, delegated=delegated),
                reply_markup=owner_broadcast.selection_keyboard(
                    draft, chats, False, delegated=delegated,
                    report_delete_after=int(state.broadcast_report_delete_after or 0)),
                parse_mode=ParseMode.HTML)
        return

    if action == "page":
        if len(parts) < 4 or not parts[3].lstrip("-").isdigit():
            await safe_answer(callback)
            return
        draft.page = max(0, int(parts[3]))
    elif action == "pick":
        if len(parts) < 5 or not parts[3].lstrip("-").isdigit():
            await safe_answer(callback)
            return
        target_chat_id = int(parts[3])
        if target_chat_id not in {int(chat.id) for chat in chats}:
            await safe_answer(callback, "⛔️ این چت در فهرست مجاز شما نیست.", show_alert=True)
            return
        selected = {int(item) for item in (draft.selected_chat_ids or [])}
        if target_chat_id in selected:
            selected.remove(target_chat_id)
        else:
            selected.add(target_chat_id)
        draft.selected_chat_ids = sorted(selected)
        draft.page = max(0, int(parts[4]))
    elif action == "all":
        draft.selected_chat_ids = [int(chat.id) for chat in chats]
    elif action == "clear":
        draft.selected_chat_ids = []
    elif action == "edit":
        draft.stage = "select"
        if len(parts) > 3 and parts[3].lstrip("-").isdigit():
            draft.page = max(0, int(parts[3]))
    elif action == "review":
        authorized_ids = {int(chat.id) for chat in chats}
        selected_ids = {int(item) for item in (draft.selected_chat_ids or [])} & authorized_ids
        if not selected_ids:
            await safe_answer(callback, "⚠️ دست‌کم یک گروه یا کانال را انتخاب کنید.", show_alert=True)
            return
        draft.selected_chat_ids = sorted(selected_ids)
        draft.stage = "review"
    elif action == "send":
        if draft.stage != "review":
            await safe_answer(callback, "⚠️ ابتدا پیش‌نمایش و مقصدها را مرور کنید.", show_alert=True)
            return
        authorized = {int(chat.id): chat for chat in chats}
        selected_ids = {int(item) for item in (draft.selected_chat_ids or [])}
        valid_chats: list[Chat] = []
        skipped_access = 0
        for chat_id in sorted(selected_ids):
            chat = authorized.get(chat_id)
            if chat is None:
                skipped_access += 1
                continue
            valid_chats.append(chat)
        if not valid_chats:
            await safe_answer(callback, "⚠️ هیچ مقصد مجازی باقی نمانده است.", show_alert=True)
            return
        await safe_answer(callback, "⏳ ارسال آغاز شد.")
        draft.stage = "sending"
        draft.updated_at = datetime.utcnow()
        await session.flush()
        # Persist the one-use state before making network calls, preventing a
        # second click from starting the same broadcast again.
        await session.commit()
        if callback.message is not None:
            await safe_edit(callback.message, "⏳ ارسال به مقصدهای تأییدشده در حال انجام است…")

        sent = failed = 0
        message_ids = [int(item) for item in (draft.source_message_ids or [])]
        for index, chat in enumerate(valid_chats):
            try:
                live_chat = await session.get(Chat, int(chat.id), populate_existing=True)
                if (live_chat is None or not live_chat.is_active
                        or live_chat.type not in {"group", "supergroup", "channel"}):
                    skipped_access += 1
                    continue
                if delegated:
                    current_grants = await owner_access.root_chat_ids(session, user_id)
                    if int(chat.id) not in current_grants or not await owner_access.is_current_admin_or_creator(
                            bot, int(chat.id), user_id):
                        skipped_access += 1
                        continue
                if draft.source_chat_id is not None and message_ids:
                    if len(message_ids) > 1:
                        await bot.copy_messages(
                            chat_id=chat.id, from_chat_id=draft.source_chat_id,
                            message_ids=message_ids)
                    else:
                        await bot.copy_message(
                            chat_id=chat.id, from_chat_id=draft.source_chat_id,
                            message_id=message_ids[0])
                elif draft.text_body:
                    await bot.send_message(
                        chat_id=chat.id, text=f"📢 اطلاعیه\n\n{draft.text_body}",
                        parse_mode=None, disable_web_page_preview=True)
                else:
                    failed += 1
                    continue
                sent += 1
            except Exception as exc:  # noqa: BLE001 - count per-chat failures safely
                failed += 1
                logger.info("ارسال همگانی به چت %s ناموفق بود: %s",
                            chat.id, exc.__class__.__name__)
            if index + 1 < len(valid_chats):
                await asyncio.sleep(0.05)

        draft.stage = "sent"
        draft.updated_at = datetime.utcnow()
        await session.commit()
        result_lines = [
            "📢 <b>ارسال همگانی پایان یافت</b>",
            f"✅ موفق: {to_persian_digits(str(sent))}",
            f"⚠️ ناموفق: {to_persian_digits(str(failed))}",
        ]
        if skipped_access:
            result_lines.append(
                f"🔐 ردشدن به‌علت تغییر دسترسی/عضویت: {to_persian_digits(str(skipped_access))}")
        if callback.message is not None:
            edited = await safe_edit(callback.message, "\n".join(result_lines),
                                     parse_mode=ParseMode.HTML)
            if edited is not None:
                if callback.message.chat.type == "private":
                    raw_delay = getattr(state, "broadcast_report_delete_after", 12)
                    delete_after = 12 if raw_delay is None else int(raw_delay)
                else:
                    delete_after = GROUP_BOT_MESSAGE_TTL
                schedule_delete(bot, callback.message.chat.id,
                                callback.message.message_id, delete_after,
                                context="broadcast_result_autodelete")
        return
    else:
        await safe_answer(callback)
        return

    draft.updated_at = datetime.utcnow()
    await session.flush()
    await safe_answer(callback)
    if callback.message is None:
        return
    if draft.stage == "review":
        await safe_edit(
            callback.message,
            owner_broadcast.review_text(draft, chats, delegated=delegated),
            reply_markup=owner_broadcast.review_keyboard(
                draft, bool(state.broadcast_enabled), delegated=delegated,
                report_delete_after=int(state.broadcast_report_delete_after or 0)),
            parse_mode=ParseMode.HTML)
    else:
        await safe_edit(
            callback.message,
            owner_broadcast._selection_text(draft, chats, bool(state.broadcast_enabled),
                                            delegated=delegated),
            reply_markup=owner_broadcast.selection_keyboard(
                draft, chats, bool(state.broadcast_enabled), delegated=delegated,
                report_delete_after=int(state.broadcast_report_delete_after or 0)),
            parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("rt:"))
async def on_root_access(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """Owner picker and capability-safe, chat-scoped root grant workflow."""
    import html

    from ..config import settings as app_settings
    from ..db.models import User
    from ..services import owner_access
    from ..keyboards.factory import cb, danger, markup

    parts = parse_cb(callback.data or "")
    action = parts[1] if len(parts) > 1 else ""
    user_id_from_callback = int(callback.from_user.id)
    is_owner = owner_access.is_bot_owner(user_id_from_callback)
    if action == "noop":
        await safe_answer(callback)
        return

    if action in {"page", "grant", "cancel", "toggle", "save"}:
        if len(parts) < 3 or not parts[2].isdigit():
            await safe_answer(callback)
            return
        draft = await owner_access.get_root_draft(
            session, int(parts[2]), user_id_from_callback)
        if draft is None:
            await safe_answer(callback, "ℹ️ این درخواست منقضی شده است.", show_alert=True)
            return

        if action in {"page", "grant"} and not is_owner:
            await safe_answer(callback, "⛔️ فقط مالک بات می‌تواند چت مقصد را انتخاب کند.",
                              show_alert=True)
            return
        if action == "cancel":
            if not is_owner:
                if callback.message is None or callback.message.chat.type not in {"group", "supergroup"}:
                    await safe_answer(callback, "⛔️ اجازهٔ لغو این درخواست را ندارید.", show_alert=True)
                    return
                if not await owner_access.can_use_root_permission(
                        bot, session, chat_id=callback.message.chat.id,
                        user_id=user_id_from_callback, permission="root_manage"):
                    await safe_answer(callback, "⛔️ اجازهٔ لغو این درخواست را ندارید.", show_alert=True)
                    return
            await session.delete(draft)
            await session.flush()
            await safe_answer(callback, "درخواست لغو شد.")
            if callback.message is not None:
                await safe_edit(callback.message, "❌ درخواست دسترسی روت لغو شد.")
            return

        if action == "page":
            chats = await owner_access.known_active_chats(session)
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            page = max(0, page)
            await safe_answer(callback)
            if callback.message is not None:
                await safe_edit(
                    callback.message, owner_access.root_picker_text(draft, chats, page),
                    reply_markup=owner_access.root_picker_keyboard(draft, chats, page),
                    parse_mode=ParseMode.HTML)
            return

        if action == "grant":
            if len(parts) < 4 or not parts[3].lstrip("-").isdigit():
                await safe_answer(callback)
                return
            chat_id = int(parts[3])
            chat = await session.get(Chat, chat_id)
            if chat is None or not chat.is_active or chat.type not in {"group", "supergroup", "channel"}:
                await safe_answer(callback, "ℹ️ این چت دیگر فعال یا شناخته‌شده نیست.", show_alert=True)
                return
            try:
                target_id, name, username = await owner_access.resolve_admin_target(
                    bot, session, chat_id, draft.target_ref)
            except owner_access.RootAccessError as exc:
                await safe_answer(callback, str(exc), show_alert=True)
                return
            if target_id in {bot.id, user_id_from_callback, app_settings.owner_id}:
                await safe_answer(callback, "⛔️ ربات، خودتان یا مالک بات را نمی‌توان هدف واگذاری قرار داد.",
                                  show_alert=True)
                return
            user = await session.get(User, target_id)
            if user is not None:
                user.first_name = name[:128]
                user.username = username
            available = owner_access.permissions_available_for_chat(chat.type)
            selected = [permission for permission in await owner_access.permissions_for(
                session, chat_id=chat_id, user_id=target_id) if permission in available]
            await safe_answer(callback)
            if callback.message is not None:
                await safe_edit(
                    callback.message,
                    owner_access.root_permission_text(
                        target_name=name, user_id=target_id, chat=chat,
                        selected_permissions=selected,
                        allowed_permissions=available),
                    reply_markup=owner_access.root_permission_keyboard(
                        draft, chat_id=chat_id, user_id=target_id,
                        selected_permissions=selected,
                        allowed_permissions=available),
                    parse_mode=ParseMode.HTML)
            return

        # Toggle/save callback format: rt:<action>:<draft>:<chat>:<target>:<mask>
        if len(parts) < 6 or not parts[3].lstrip("-").isdigit() or not parts[4].isdigit() or not parts[5].isdigit():
            await safe_answer(callback)
            return
        chat_id, target_id, mask = int(parts[3]), int(parts[4]), int(parts[5])
        chat = await session.get(Chat, chat_id)
        if chat is None or not chat.is_active or chat.type not in {"group", "supergroup", "channel"}:
            await safe_answer(callback, "ℹ️ این چت دیگر فعال یا شناخته‌شده نیست.", show_alert=True)
            return
        if not is_owner:
            if (callback.message is None
                    or callback.message.chat.type not in {"group", "supergroup"}
                    or int(callback.message.chat.id) != chat_id):
                await safe_answer(callback, "⛔️ این واگذاری فقط از همان گروه قابل مدیریت است.",
                                  show_alert=True)
                return
            if not await owner_access.can_use_root_permission(
                    bot, session, chat_id=chat_id, user_id=user_id_from_callback,
                    permission="root_manage"):
                await safe_answer(callback, "⛔️ دسترسی «مدیریت روت» در این گروه ندارید.",
                                  show_alert=True)
                return
            delegated_permissions = await owner_access.permissions_for(
                session, chat_id=chat_id, user_id=user_id_from_callback)
            available = owner_access.permissions_available_for_chat(chat.type)
            allowed = [permission for permission in delegated_permissions
                       if permission in available]
        else:
            allowed = owner_access.permissions_available_for_chat(chat.type)
        valid_mask = owner_access.permission_mask(allowed)
        if mask & ~valid_mask:
            await safe_answer(callback, "⛔️ این اختیار به شما واگذار نشده است.", show_alert=True)
            return
        try:
            resolved_id, name, username = await owner_access.resolve_admin_target(
                bot, session, chat_id, draft.target_ref)
        except owner_access.RootAccessError as exc:
            await safe_answer(callback, str(exc), show_alert=True)
            return
        if resolved_id != target_id:
            await safe_answer(callback, "⚠️ وضعیت هدف تغییر کرده است؛ درخواست را دوباره آغاز کنید.",
                              show_alert=True)
            return
        if target_id in {bot.id, user_id_from_callback, app_settings.owner_id}:
            await safe_answer(callback, "⛔️ ربات، خودتان یا مالک بات را نمی‌توان هدف واگذاری قرار داد.",
                              show_alert=True)
            return
        selected = owner_access.permissions_from_mask(
            mask, allowed_permissions=allowed)
        if action == "toggle":
            await safe_answer(callback)
            if callback.message is not None:
                await safe_edit(
                    callback.message,
                    owner_access.root_permission_text(
                        target_name=name, user_id=target_id, chat=chat,
                        selected_permissions=selected,
                        allowed_permissions=allowed),
                    reply_markup=owner_access.root_permission_keyboard(
                        draft, chat_id=chat_id, user_id=target_id,
                        selected_permissions=selected,
                        allowed_permissions=allowed),
                    parse_mode=ParseMode.HTML)
            return
        if not selected:
            await safe_answer(callback, "❌ دست‌کم یک اختیار را تیک بزنید.", show_alert=True)
            return
        created = await owner_access.grant_root(
            session, chat_id=chat_id, user_id=target_id,
            granted_by=user_id_from_callback, permissions=selected)
        user = await session.get(User, target_id)
        if user is None:
            user = User(id=target_id, first_name=name[:128], username=username)
            session.add(user)
        else:
            user.first_name = name[:128]
            user.username = username
        await session.delete(draft)
        await session.flush()
        await safe_answer(callback, "✅ اختیارات ذخیره شد." if created else "✅ اختیارات به‌روزرسانی شد.")
        if callback.message is not None:
            message_text = (
                f"✅ اختیارات روت برای {html.escape(name)} ذخیره شد.\n\n"
                f"💬 چت: {html.escape(chat.title or str(chat.id))}\n"
                f"🔑 {html.escape(owner_access.selected_permissions_text(selected))}\n\n"
                "🔒 این دسترسی به همین چت محدود است؛ اثر بن/سکوت سراسری فقط در مقصدهای واجد شرایط اعمال می‌شود.")
            await safe_edit(
                callback.message, message_text,
                reply_markup=markup([[danger("لغو همین دسترسی", cb("rt", "revoke", chat_id, target_id))]]),
                parse_mode=ParseMode.HTML)
        return

    if action == "revoke":
        if len(parts) < 4 or not parts[2].lstrip("-").isdigit() or not parts[3].isdigit():
            await safe_answer(callback)
            return
        chat_id, target_id = int(parts[2]), int(parts[3])
        if not is_owner:
            if (callback.message is None
                    or callback.message.chat.type not in {"group", "supergroup"}
                    or int(callback.message.chat.id) != chat_id
                    or not await owner_access.can_use_root_permission(
                        bot, session, chat_id=chat_id, user_id=user_id_from_callback,
                        permission="root_manage")):
                await safe_answer(callback, "⛔️ فقط مالک یا دارندهٔ اختیار مدیریت روت در همین گروه می‌تواند لغو کند.",
                                  show_alert=True)
                return
        removed = await owner_access.revoke_root(
            session, chat_id=chat_id, user_id=target_id)
        await safe_answer(callback, "🗑 دسترسی لغو شد." if removed else "ℹ️ دسترسی از قبل برداشته شده بود.")
        if callback.message is None:
            return
        if callback.message.chat.type != "private":
            await safe_edit(callback.message, "🗑 دسترسی روت همین چت لغو شد.")
            return
        grants = await owner_access.list_root_grants(session)
        if not grants:
            await safe_edit(callback.message, "✅ هیچ دسترسی روت فعالی وجود ندارد.")
            return
        lines = ["🔐 <b>دسترسی‌های روت</b>", ""]
        buttons = []
        for grant, chat, user in grants[:50]:
            member_name = (f"{user.first_name or ''} {user.last_name or ''}".strip()
                           or (f"@{user.username}" if user.username else str(user.id)))
            chat_name = chat.title or (f"@{chat.username}" if chat.username else str(chat.id))
            labels = owner_access.selected_permissions_text(grant.permissions)
            lines.append(f"• {html.escape(member_name)} — {html.escape(chat_name)}\n"
                         f"   🔑 {html.escape(labels)}")
            buttons.append([danger(
                f"🗑 {member_name[:18]} · {chat_name[:18]}",
                cb("rt", "revoke", chat.id, grant.user_id))])
        await safe_edit(callback.message, "\n".join(lines), reply_markup=markup(buttons),
                        parse_mode=ParseMode.HTML)
        return

    await safe_answer(callback)


@router.callback_query.middleware()
async def panel_owner_middleware(handler, event: CallbackQuery, data: dict):
    """Keep group panels persistent and bound to the admin who opened them.

    A stale panel whose in-memory owner record was lost on restart is left on
    screen but disabled; the admin can open a fresh panel. This avoids both a
    disappearing panel and accidentally granting a stale keyboard to others.
    """
    message = getattr(event, "message", None)
    user = getattr(event, "from_user", None)
    if message is not None and user is not None:
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        message_id = getattr(message, "message_id", None)
        is_group = getattr(chat, "type", None) in {"group", "supergroup"}
        panel_keyboard = is_management_panel_markup(getattr(message, "reply_markup", None))
        known_panel = bool(chat_id is not None and message_id is not None
                           and is_panel_message(chat_id, message_id))
        group_panel = is_group and (panel_keyboard or known_panel)
        if group_panel:
            remember_panel_message(chat_id, message_id)

        owner = _panel_owner(chat_id, message_id) if chat_id is not None and message_id is not None else None
        bot = data.get("bot")
        if group_panel and owner is None:
            if bot is not None:
                await safe_call(
                    lambda: bot.answer_callback_query(
                        callback_query_id=event.id,
                        text=("⌛ این پنل قدیمی است و برای امنیت غیرفعال مانده؛ "
                              "در گروه دوباره دستور «پنل» را بزنید."),
                        show_alert=True),
                    default=None, context="stale_panel_denied")
            return None
        if owner is not None and owner != user.id:
            if bot is not None:
                await safe_call(
                    lambda: bot.answer_callback_query(
                        callback_query_id=event.id,
                        text=("🔒 این پنل مخصوص مدیری است که آن را باز کرده است.\n"
                              "برای پنل خودتان در گروه دستور «پنل» را بزنید."),
                        show_alert=True),
                    default=None, context="panel_owner_denied")
            return None
    return await handler(event, data)
