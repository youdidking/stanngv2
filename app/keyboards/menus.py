"""Role-aware panel menus.

Every sub-menu is rendered from the current settings so the message can be
edited in place (no message spam), and every sub-menu offers
``⬅️ بازگشت`` + ``🏠 خانه``.
"""

from __future__ import annotations

from typing import Any, Callable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..core.normalization import to_persian_digits
from ..services.chatlock import MODE_LABELS
from ..services.leave_guard import status_text as leave_status_text
from ..services.locks import LOCK_GROUPS, LOCK_REGISTRY
from ..services.moderation import PUNISHMENT_OPTIONS_FA
from ..services.roles import level_of
from .factory import (  # noqa: F401 - re-exported for handlers
    DEFAULT,
    DANGER,
    PRIMARY,
    SUCCESS,
    btn,
    cb,
    close,
    danger,
    grid,
    home,
    markup,
    onoff,
    primary,
    row,
    success,
)

BOT_NAME = "Armando Group Manager"


def creator_button() -> InlineKeyboardButton:
    """Always visible credit link to the bot creator."""
    from ..config import settings

    username = (settings.creator_username or "").lstrip("@")
    url = settings.creator_url or (f"https://t.me/{username}" if username else "")
    return InlineKeyboardButton(text="👤 سازنده: @RVIVL", url=url or "https://t.me/RVIVL")


def with_creator(keyboard: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Append the creator link as the last row of any keyboard."""
    rows = [list(row) for row in keyboard.inline_keyboard]
    rows.append([creator_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# --------------------------------------------------------------------------- #
# Section registry
# --------------------------------------------------------------------------- #
SECTIONS: dict[str, tuple[str, str, str]] = {
    # key: (emoji + Persian title, minimum role, parent)
    "main": ("🏠 منوی اصلی", "member", ""),
    "users": ("🛡 مدیریت کاربران", "moderator", "main"),
    "security": ("🔐 امنیت و ضداسپم", "admin", "main"),
    "locks": ("🔒 قفل‌ها", "admin", "main"),
    "filters": ("📝 فیلتر و یادداشت", "moderator", "main"),
    "welcome": ("👋 ورود و خروج", "admin", "main"),
    "messages": ("📌 پیام‌ها", "helper", "main"),
    "stats": ("📊 آمار", "member", "main"),
    "members": ("👥 اعضا", "member", "main"),
    "fun": ("🎮 سرگرمی", "member", "main"),
    "settings": ("⚙️ تنظیمات", "admin", "main"),
    "help": ("📚 راهنما", "member", "main"),
    "profile": ("👤 پنل من", "member", "main"),
    # second level panels
    "warnings": ("⚠️ تنظیمات اخطار", "admin", "settings"),
    "captcha": ("🤖 تنظیمات کپچا", "admin", "settings"),
    "antispam": ("🚫 ضداسپم", "admin", "settings"),
    "night": ("🌙 حالت شب", "admin", "settings"),
    "raid": ("🛡 ضد Raid", "admin", "settings"),
    "staff": ("👮 نقش‌ها", "admin", "settings"),
    "trust": ("⭐️ کاربران ویژه", "admin", "settings"),
    "logs": ("📜 لاگ‌ها", "admin", "settings"),
    "schedule": ("⏰ زمان‌بندی", "admin", "settings"),
    "federation": ("🌐 فدراسیون", "admin", "settings"),
    "backup": ("💾 پشتیبان‌گیری", "admin", "settings"),
    "disabled": ("🚫 بخش‌های غیرفعال", "admin", "settings"),
    "tags": ("🏷 تگ‌ها", "admin", "settings"),
    "rules": ("📜 قوانین", "admin", "settings"),
    "ai_chat": ("💬 گفت‌وگوی هوشمند", "admin", "settings"),
}

MAIN_ORDER = ["users", "security", "locks", "filters", "welcome", "messages",
              "stats", "members", "fun", "settings", "help"]
SETTINGS_ORDER = ["security", "ai_chat", "locks", "filters", "welcome", "warnings", "captcha",
                  "antispam", "raid", "night", "tags", "rules", "reports", "staff",
                  "trust", "logs", "schedule", "federation", "backup", "disabled"]

ROLE_LABELS = {
    "founder": "🏆 مالک",
    "cofounder": "💎 هم‌بنیان‌گذار",
    "super_admin": "👑 مدیر ارشد",
    "admin": "👮 مدیر",
    "moderator": "🛡 ناظر",
    "helper": "🤝 کمک‌یار",
    "muter": "🔇 ساکت‌کننده",
    "cleaner": "🧹 پاکساز",
    "trusted": "⭐️ کاربر ویژه",
    "member": "👤 عضو",
}


def allowed_sections(level: int) -> list[str]:
    return [key for key in MAIN_ORDER if level >= level_of(SECTIONS[key][1])]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def state_icon(value: bool) -> str:
    return "🟢" if value else "🔴"


def state_text(label: str, value: bool) -> str:
    return f"{label}: {'🟢 فعال' if value else '🔴 خاموش'}"


def toggle_button(label: str, field: str, value: bool, panel: str) -> Any:
    return onoff(f"{label}  {state_icon(value)}", value, cb("set", field, int(not value), panel))


def reset_button(section: str, label: str = "♻️ بازگشت این بخش به پیش‌فرض") -> list[Any]:
    """A per-section reset entry; the panel handler always asks for confirmation."""
    return [danger(label, cb("reset", "ask", section))]


def number_buttons(label: str, field: str, value: int, panel: str, *,
                   step: int = 1, minimum: int = 0, maximum: int = 9999) -> list[Any]:
    return [
        primary(f"➖ {label}", cb("num", field, -step, panel, minimum, maximum)),
        btn(f"{to_persian_digits(str(value))}", cb("noop")),
        success(f"➕ {label}", cb("num", field, step, panel, minimum, maximum)),
    ]


def action_selector(panel: str, field: str, current: str, *,
                    options: dict[str, str] | None = None,
                    prefix: str = "set") -> list[list[Any]]:
    options = options or PUNISHMENT_OPTIONS_FA
    buttons = []
    for key, label in options.items():
        style = SUCCESS if key == current else None
        buttons.append(btn(("✅ " if key == current else "") + label,
                           cb(prefix, field, key, panel), style=style))
    return grid(buttons, per_row=2)


# --------------------------------------------------------------------------- #
# Main menu
# --------------------------------------------------------------------------- #
def main_menu(level: int, *, chat_type: str = "group") -> InlineKeyboardMarkup:
    sections = allowed_sections(level)
    buttons = []
    for key in sections:
        label = SECTIONS[key][0]
        buttons.append(primary(label, cb("panel", key)))
    rows = grid(buttons, per_row=2)
    if chat_type == "group":
        rows.append([primary("👤 پنل اختصاصی من", cb("panel", "profile"))])
    rows.append([close()])
    return markup(rows)


def main_menu_text(chat_title: str = "", role_label: str = "") -> str:
    lines = [f"🤖 <b>{BOT_NAME}</b>", ""]
    if chat_title:
        lines.append(f"💬 گروه: {chat_title}")
    if role_label:
        lines.append(f"🎖 سمت شما: {role_label}")
    lines += ["", "یکی از بخش‌های زیر را انتخاب کنید:"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Section panels
# --------------------------------------------------------------------------- #
def _panel_users(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    text = "\n".join([
        "🛡 <b>مدیریت کاربران</b>",
        "",
        state_text("سقف اخطار", True) .replace("سقف اخطار", f"سقف اخطار: {to_persian_digits(str(settings.get('warn_limit', 4)))}"),
        state_text("اقدام در سقف", True).replace("اقدام در سقف",
                                                 f"اقدام در سقف: {PUNISHMENT_OPTIONS_FA.get(settings.get('warn_action', 'mute'), settings.get('warn_action', 'mute'))}"),
        "",
        "برای اقدام روی یک کاربر، روی پیام او <b>ریپلای</b> کنید و دستور را بفرستید.",
        "مثال‌ها:",
        "• <code>بن</code> • <code>بن ۲روز تبلیغ</code> • <code>سکوت ۳۰دقیقه</code>",
        "• <code>اخطار تبلیغ</code> • <code>کیک</code> • <code>کسر اخطار</code>",
    ])
    keyboard = markup([
        [primary("⚠️ وضعیت اخطار", cb("panel", "warnings"))],
        [primary("🔨 بن", cb("cmd", "ban")), primary("🔇 سکوت", cb("cmd", "mute")),
         primary("⚠️ اخطار", cb("cmd", "warn"))],
        [primary("👢 اخراج", cb("cmd", "kick")), success("🕊 رفع بن", cb("cmd", "unban")),
         success("🔊 لغو سکوت", cb("cmd", "unmute"))],
        [primary("🗑 کسر اخطار", cb("cmd", "unwarn")), primary("♻️ صفر کردن اخطار", cb("cmd", "resetwarn"))],
        [primary("📜 تاریخچه کاربر", cb("cmd", "history")), primary("ℹ️ اطلاعات کاربر", cb("cmd", "info"))],
        [primary("🏷 تغییر تگ کاربر", cb("panel", "tags"))],
        [danger("🚫 پاکسازی کل اخطارهای گروه", cb("conf", "clearwarns", "all"))],
        row(home()),
    ])
    return text, keyboard


def _panel_security(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    locks = ctx.get("locks") or {}
    profanity_enabled = bool(settings.get("anti_profanity_enabled"))
    porn_enabled = bool(settings.get("anti_porn_enabled"))
    links_lock = locks.get("links")
    text = "\n".join([
        "🔐 <b>امنیت و ضداسپم</b>",
        "",
        state_text("ضداسپم", bool(settings.get("antispam_enabled"))),
        state_text("ضدفلاود", bool(settings.get("antiflood_enabled"))),
        state_text("ضدلینک", bool(links_lock and links_lock.enabled)),
        state_text("ضدفحاشی", profanity_enabled),
        state_text("ضدپورن", porn_enabled),
        state_text("ضد Raid", bool(settings.get("antiraid_enabled"))),
        state_text("CAPTCHA", bool(settings.get("captcha_enabled"))),
        state_text("تحلیل هوشمند پیام‌ها", bool(settings.get("ai_enabled"))),
        state_text("حالت شب", bool(settings.get("night_mode_enabled"))),
    ])
    panel = "security"
    keyboard = markup([
        [toggle_button("🚫 ضداسپم", "antispam_enabled", bool(settings.get("antispam_enabled")), panel),
         toggle_button("🌊 ضدفلاود", "antiflood_enabled", bool(settings.get("antiflood_enabled")), panel)],
        [toggle_button("🤬 ضدفحاشی", "anti_profanity_enabled", profanity_enabled, panel),
         toggle_button("🔞 ضدپورن", "anti_porn_enabled", porn_enabled, panel)],
        [toggle_button("🛡 ضد Raid", "antiraid_enabled", bool(settings.get("antiraid_enabled")), panel),
         toggle_button("🤖 CAPTCHA", "captcha_enabled", bool(settings.get("captcha_enabled")), panel)],
        [toggle_button("🧠 تحلیل هوشمند پیام‌ها", "ai_enabled", bool(settings.get("ai_enabled")), panel),
         toggle_button("🌙 حالت شب", "night_mode_enabled", bool(settings.get("night_mode_enabled")), panel)],
        [primary("💬 گفت‌وگوی هوشمند", cb("panel", "ai_chat"))],
        [primary("🌊 تنظیمات ضدفلاود", cb("panel", "antispam")),
         primary("⚠️ تنظیمات اخطار", cb("panel", "warnings"))],
        [primary("🤖 تنظیمات کپچا", cb("panel", "captcha")),
         primary("🛡 تنظیمات ضد Raid", cb("panel", "raid"))],
        [primary("🔒 قفل‌ها", cb("panel", "locks")), primary("🌙 حالت شب", cb("panel", "night"))],
        reset_button("security", "♻️ بازنشانی تنظیمات امنیتی"),
        row(home()),
    ])
    return text, keyboard


def _panel_locks(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    locks = ctx.get("locks") or {}
    settings = ctx.get("settings") or {}
    page = ctx.get("lock_page") or "links"
    keys = [k for k, spec in LOCK_REGISTRY.items() if spec.group == page]
    lines = [f"🔒 <b>قفل‌ها — {LOCK_GROUPS.get(page, page)}</b>", ""]
    for key in keys:
        spec = LOCK_REGISTRY[key]
        state = locks.get(key)
        enabled = bool(state and state.enabled)
        action = (state.action if state else spec.default_action)
        lines.append(f"{spec.emoji} {spec.label_fa}  {state_icon(enabled)}"
                     + (f"  (🎯 {PUNISHMENT_OPTIONS_FA.get(action, action)})" if enabled else ""))
    lines.append("")
    lines.append("برای تغییر، روی نام قفل بزنید؛ سپس نوع برخورد را انتخاب کنید.")
    lines.append("")
    lines.append("🔒 <b>قفل کلی گروه</b> (دسترسی ارسال اعضا در تلگرام)")
    lines.append(f"وضعیت: {MODE_LABELS.get(ctx.get('chat_lock') or 'off', '—')}")
    lines.append("")
    lines.append("🚪 <b>قفل خروج</b>")
    lines.append(leave_status_text(settings))
    lines.append("📨 هر بن موفق، در هر گروه، با جزئیات کامل به پی‌وی مالک ربات و دارندگان روت همان گروه فرستاده می‌شود؛ پیام گروهی پس از ۳۰ ثانیه پاک می‌شود.")
    if settings.get("leave_notify_owner"):
        lines.append("📬 گزارش خصوصیِ خروج‌هایی که به بن منجر نشده‌اند نیز روشن است.")

    buttons = []
    for key in keys:
        spec = LOCK_REGISTRY[key]
        state = locks.get(key)
        enabled = bool(state and state.enabled)
        buttons.append(onoff(f"{spec.emoji} {spec.label_fa}  {state_icon(enabled)}", enabled,
                             cb("lock", "toggle", key, page)))
    rows = grid(buttons, per_row=2)

    rows.append([danger("🔒 قفل گروه", cb("cl", "all")),
                 primary("🖼 قفل رسانه", cb("cl", "media")),
                 success("🔓 باز کردن گروه", cb("cl", "off"))])
    leave_rows = [
        [toggle_button("🚪 بن هنگام خروج", "ban_on_leave",
                       bool(settings.get("ban_on_leave")), "locks"),
         toggle_button("⚡️ خروج سریع", "quick_leave_ban",
                       bool(settings.get("quick_leave_ban")), "locks")],
    ]
    if settings.get("quick_leave_ban"):
        leave_rows.append(number_buttons("ثانیه", "quick_leave_seconds",
                                         int(settings.get("quick_leave_seconds") or 10),
                                         "locks", step=5, minimum=3, maximum=600))
    leave_rows.append([toggle_button("📬 گزارش خروج بدون بن", "leave_notify_owner",
                                     bool(settings.get("leave_notify_owner")), "locks")])
    rows.extend(leave_rows)
    group_buttons = [primary(LOCK_GROUPS[g], cb("lockpage", g)) for g in LOCK_GROUPS]
    rows.append(group_buttons)
    rows.append([danger("🧹 غیرفعال‌سازی همه قفل‌ها", cb("conf", "clearlocks", "all"))])
    rows.append(reset_button("locks", "♻️ بازنشانی قفل‌ها و خروج"))
    rows.append(row(home()))
    return "\n".join(lines), markup(rows)


def _panel_lock_detail(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    key = ctx.get("lock_key") or "links"
    locks = ctx.get("locks") or {}
    spec = LOCK_REGISTRY.get(key)
    state = locks.get(key)
    enabled = bool(state and state.enabled)
    action = state.action if state else (spec.default_action if spec else "delete")
    lines = [
        f"{spec.emoji} <b>{spec.label_fa}</b>",
        "",
        f"وضعیت: {state_icon(enabled)}",
        f"برخورد: {PUNISHMENT_OPTIONS_FA.get(action, action)}",
    ]
    if spec and spec.threshold:
        value = int((state.extra or {}).get(spec.threshold_field or "", spec.default_threshold)) if state else spec.default_threshold
        lines.append(f"حد مجاز: {to_persian_digits(str(value))}")
    rows = [
        [onoff(f"{'🟢 فعال' if enabled else '🔴 خاموش'}", enabled, cb("lock", "toggle", key, ctx.get('lock_page') or 'links'))],
        *action_selector("lock", f"action:{key}", action, options=PUNISHMENT_OPTIONS_FA, prefix="lockact"),
    ]
    if spec and spec.threshold:
        field = spec.threshold_field or "value"
        value = int((state.extra or {}).get(field, spec.default_threshold)) if state else spec.default_threshold
        step = 500 if value >= 1000 else (5 if value <= 50 else 50)
        rows.append([
            primary("➖", cb("locknum", key, field, -step, ctx.get("lock_page") or "links")),
            btn(f"حد مجاز: {to_persian_digits(str(value))}", cb("noop")),
            success("➕", cb("locknum", key, field, step, ctx.get("lock_page") or "links")),
        ])
    rows.append(row(home()))
    keyboard = markup(rows)
    return "\n".join(lines), keyboard


def _panel_filters(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    counts = ctx.get("filter_counts") or {"blocklist": 0, "custom": 0, "notes": 0, "commands": 0}
    text = "\n".join([
        "📝 <b>فیلتر، یادداشت و دستورات</b>",
        "",
        f"🚫 کلمات فیلتر: {to_persian_digits(str(counts.get('blocklist', 0)))}",
        f"✨ پاسخ‌های خودکار: {to_persian_digits(str(counts.get('custom', 0)))}",
        f"📒 یادداشت‌ها: {to_persian_digits(str(counts.get('notes', 0)))}",
        f"⌨️ دستورات شخصی: {to_persian_digits(str(counts.get('commands', 0)))}",
        "",
        "نمونه دستورها:",
        "• <code>فیلتر کردن تبلیغ</code>",
        "• <code>حذف فیلتر تبلیغ</code> • <code>لیست فیلتر</code>",
        "• <code>ذخیره قوانین = متن قوانین...</code> • <code>گرفتن قوانین</code>",
    ])
    keyboard = markup([
        [primary("🚫 لیست فیلترها", cb("cmd", "listfilters")),
         primary("📒 لیست یادداشت‌ها", cb("cmd", "listnotes"))],
        [primary("⌨️ دستورات شخصی", cb("cmd", "listcommands")),
         primary("🎴 محرک‌های استیکر", cb("cmd", "listmagic"))],
        [danger("🧹 حذف همه فیلترها", cb("conf", "clearfilters", "blocklist")),
         danger("🧹 حذف همه یادداشت‌ها", cb("conf", "clearnotes", "all"))],
        reset_button("filters", "♻️ بازگردانی تنظیمات پیش‌فرض فیلتر"),
        row(home()),
    ])
    return text, keyboard


def _panel_welcome(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    panel = "welcome"
    text = "\n".join([
        "👋 <b>ورود و خروج</b>",
        "",
        state_text("پیام خوشامد", bool(settings.get("welcome_enabled"))),
        state_text("پیام خداحافظی", bool(settings.get("goodbye_enabled"))),
        state_text("حذف پیام ورود", bool(settings.get("clean_join"))),
        state_text("حذف پیام خروج", bool(settings.get("clean_leave"))),
        state_text("حذف اعلان پین", bool(settings.get("clean_pin"))),
        state_text("حذف سایر پیام‌های سرویسی", bool(settings.get("clean_service"))),
        state_text("حذف پست کانال", bool(settings.get("clean_channel_post"))),
        "",
        "نمونه:",
        "• <code>تنظیم خوشامد سلام {first_name} عزیز</code>",
        "• <code>تنظیم خداحافظی خداحافظ {first_name}</code>",
    ])
    keyboard = markup([
        [toggle_button("🌸 خوشامد", "welcome_enabled", bool(settings.get("welcome_enabled")), panel),
         toggle_button("👋 خداحافظی", "goodbye_enabled", bool(settings.get("goodbye_enabled")), panel)],
        [toggle_button("🧹 حذف ورود", "clean_join", bool(settings.get("clean_join")), panel),
         toggle_button("🧹 حذف خروج", "clean_leave", bool(settings.get("clean_leave")), panel)],
        [toggle_button("📌 حذف اعلان پین", "clean_pin", bool(settings.get("clean_pin")), panel),
         toggle_button("🧩 حذف سرویسی", "clean_service", bool(settings.get("clean_service")), panel)],
        [toggle_button("📣 حذف پست کانال", "clean_channel_post", bool(settings.get("clean_channel_post")), panel)],
        [primary("🤖 تنظیمات کپچا", cb("panel", "captcha"))],
        [danger("♻️ بازگشت خوشامد به پیش‌فرض", cb("reset", "ask", "welcome"))],
        row(home()),
    ])
    return text, keyboard


def _panel_warnings(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    limit = int(settings.get("warn_limit", 4) or 4)
    action = settings.get("warn_action", "mute") or "mute"
    text = "\n".join([
        "⚠️ <b>تنظیمات اخطار</b>",
        "",
        f"🔢 سقف اخطار: {to_persian_digits(str(limit))}",
        f"🎯 اقدام هنگام رسیدن به سقف: {PUNISHMENT_OPTIONS_FA.get(action, action)}",
        f"⏱ مدت اقدام موقت: {to_persian_digits(str(settings.get('warn_action_duration') or 0))} دقیقه",
        f"📅 انقضای اخطار: " + (f"{to_persian_digits(str(settings.get('warn_expire_days') or 0))} روز"
                                if int(settings.get("warn_expire_days") or 0) > 0 else "بدون انقضا"),
    ])
    keyboard = markup([
        [btn(f"🔢 سقف: {to_persian_digits(str(limit))}", cb("noop"))],
        [primary("۳", cb("set", "warn_limit", 3, "warnings")),
         primary("۴", cb("set", "warn_limit", 4, "warnings")),
         primary("۵", cb("set", "warn_limit", 5, "warnings")),
         primary("۷", cb("set", "warn_limit", 7, "warnings")),
         primary("۱۰", cb("set", "warn_limit", 10, "warnings"))],
        [btn("🎯 اقدام:", cb("noop"))],
        *action_selector("warnings", "warn_action", action),
        [btn("⏱ مدت (دقیقه):", cb("noop"))],
        number_buttons("دقیقه", "warn_action_duration", int(settings.get("warn_action_duration") or 60),
                       "warnings", step=30, minimum=1, maximum=10080),
        [btn("📅 انقضا (روز):", cb("noop"))],
        number_buttons("روز", "warn_expire_days", int(settings.get("warn_expire_days") or 0),
                       "warnings", step=1, minimum=0, maximum=365),
        [danger("🚫 پاکسازی کل اخطارهای گروه", cb("conf", "clearwarns", "all"))],
        reset_button("warnings", "♻️ بازنشانی تنظیمات اخطار"),
        row(home()),
    ])
    return text, keyboard


def _panel_captcha(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    panel = "captcha"
    mode = settings.get("captcha_mode", "button") or "button"
    modes = {"button": "دکمه‌ای", "math": "ریاضی", "emoji": "ایموجی"}
    fail = settings.get("captcha_fail_action", "kick") or "kick"
    text = "\n".join([
        "🤖 <b>تنظیمات کپچا</b>",
        "",
        state_text("وضعیت", bool(settings.get("captcha_enabled"))),
        f"🧩 نوع چالش: {modes.get(mode, mode)}",
        f"⏱ مهلت پاسخ: {to_persian_digits(str(settings.get('captcha_timeout', 120)))} ثانیه",
        f"🎯 تلاش مجاز: {to_persian_digits(str(settings.get('captcha_max_attempts', 3)))}",
        f"🚫 اقدام پس از شکست: {PUNISHMENT_OPTIONS_FA.get(fail, fail)}",
    ])
    keyboard = markup([
        [toggle_button("🤖 کپچا", "captcha_enabled", bool(settings.get("captcha_enabled")), panel)],
        [btn("🧩 نوع چالش:", cb("noop"))],
        [onoff("دکمه‌ای", mode == "button", cb("set", "captcha_mode", "button", panel)),
         onoff("ریاضی", mode == "math", cb("set", "captcha_mode", "math", panel)),
         onoff("ایموجی", mode == "emoji", cb("set", "captcha_mode", "emoji", panel))],
        [btn("⏱ مهلت (ثانیه):", cb("noop"))],
        number_buttons("ثانیه", "captcha_timeout", int(settings.get("captcha_timeout") or 120),
                       panel, step=30, minimum=30, maximum=900),
        [btn("🎯 تلاش مجاز:", cb("noop"))],
        number_buttons("تلاش", "captcha_max_attempts", int(settings.get("captcha_max_attempts") or 3),
                       panel, step=1, minimum=1, maximum=10),
        [btn("🚫 اقدام پس از شکست:", cb("noop"))],
        *action_selector(panel, "captcha_fail_action", fail,
                         options={"kick": "اخراج", "ban": "بن", "mute": "سکوت"}),
        reset_button("captcha", "♻️ بازنشانی تنظیمات کپچا"),
        row(home()),
    ])
    return text, keyboard


def _panel_antispam(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    panel = "antispam"
    flood_action = settings.get("antiflood_action", "mute")
    text = "\n".join([
        "🚫 <b>ضداسپم و ضدفلاود</b>",
        "",
        state_text("ضداسپم", bool(settings.get("antispam_enabled"))),
        state_text("ضدفلاود", bool(settings.get("antiflood_enabled"))),
        f"🌊 آستانه فلاود: {to_persian_digits(str(settings.get('antiflood_count', 5)))} پیام در "
        f"{to_persian_digits(str(settings.get('antiflood_window', 3)))} ثانیه",
        f"🎯 اقدام فلاود: {PUNISHMENT_OPTIONS_FA.get(flood_action, flood_action)}",
        f"🔁 تکرار پیام: " + (f"{to_persian_digits(str(settings.get('antispam_same_limit')))} بار"
                              if int(settings.get("antispam_same_limit") or 0) > 0 else "خاموش"),
        f"📣 منشن اسپم: " + (f"{to_persian_digits(str(settings.get('mention_spam_limit')))} منشن"
                             if int(settings.get("mention_spam_limit") or 0) > 0 else "خاموش"),
    ])
    keyboard = markup([
        [toggle_button("🚫 ضداسپم", "antispam_enabled", bool(settings.get("antispam_enabled")), panel),
         toggle_button("🌊 ضدفلاود", "antiflood_enabled", bool(settings.get("antiflood_enabled")), panel)],
        [btn("🌊 تعداد پیام:", cb("noop"))],
        number_buttons("پیام", "antiflood_count", int(settings.get("antiflood_count") or 5),
                       panel, step=1, minimum=2, maximum=50),
        [btn("⏱ پنجره زمانی (ثانیه):", cb("noop"))],
        number_buttons("ثانیه", "antiflood_window", int(settings.get("antiflood_window") or 3),
                       panel, step=1, minimum=1, maximum=120),
        [btn("🎯 اقدام فلاود:", cb("noop"))],
        *action_selector(panel, "antiflood_action", flood_action),
        [btn("🔁 تکرار پیام (۰ = خاموش):", cb("noop"))],
        number_buttons("بار", "antispam_same_limit", int(settings.get("antispam_same_limit") or 0),
                       panel, step=1, minimum=0, maximum=20),
        [btn("📣 منشن اسپم (۰ = خاموش):", cb("noop"))],
        number_buttons("منشن", "mention_spam_limit", int(settings.get("mention_spam_limit") or 0),
                       panel, step=1, minimum=0, maximum=30),
        reset_button("antispam", "♻️ بازنشانی تنظیمات ضداسپم"),
        row(home()),
    ])
    return text, keyboard


def _panel_raid(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    panel = "raid"
    action = settings.get("antiraid_action", "kick")
    text = "\n".join([
        "🛡 <b>ضد Raid</b>",
        "",
        state_text("وضعیت", bool(settings.get("antiraid_enabled"))),
        f"📈 آستانه: {to_persian_digits(str(settings.get('antiraid_threshold', 8)))} ورود در "
        f"{to_persian_digits(str(settings.get('antiraid_window', 60)))} ثانیه",
        f"🎯 اقدام: {PUNISHMENT_OPTIONS_FA.get(action, action)}",
        state_text("محدودسازی اعضای جدید", bool(settings.get("antiraid_lock_new"))),
        state_text("اجبار کپچا هنگام رید", bool(settings.get("antiraid_force_captcha"))),
    ])
    keyboard = markup([
        [toggle_button("🛡 ضد Raid", "antiraid_enabled", bool(settings.get("antiraid_enabled")), panel)],
        [btn("📈 تعداد ورود:", cb("noop"))],
        number_buttons("ورود", "antiraid_threshold", int(settings.get("antiraid_threshold") or 8),
                       panel, step=1, minimum=2, maximum=100),
        [btn("⏱ پنجره (ثانیه):", cb("noop"))],
        number_buttons("ثانیه", "antiraid_window", int(settings.get("antiraid_window") or 60),
                       panel, step=10, minimum=10, maximum=3600),
        [btn("🎯 اقدام:", cb("noop"))],
        *action_selector(panel, "antiraid_action", action,
                         options={"kick": "اخراج", "ban": "بن", "mute": "سکوت", "none": "فقط هشدار"}),
        [toggle_button("🔒 محدودسازی جدیدها", "antiraid_lock_new",
                       bool(settings.get("antiraid_lock_new")), panel),
         toggle_button("🤖 اجبار کپچا", "antiraid_force_captcha",
                       bool(settings.get("antiraid_force_captcha")), panel)],
        reset_button("raid", "♻️ بازنشانی تنظیمات ضد Raid"),
        row(home()),
    ])
    return text, keyboard


def _panel_night(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    panel = "night"
    action = settings.get("night_action", "mute")
    text = "\n".join([
        "🌙 <b>حالت شب</b>",
        "",
        state_text("وضعیت", bool(settings.get("night_mode_enabled"))),
        f"⏰ بازه: {to_persian_digits(settings.get('night_start', '01:00'))} تا "
        f"{to_persian_digits(settings.get('night_end', '06:00'))}",
        f"🎯 اقدام: {PUNISHMENT_OPTIONS_FA.get(action, action)}",
        "🔓 سکوت/بن/اخراجِ شبانه فقط تا پایان بازه است و سپس خودکار آزاد می‌شود.",
        "",
        "برای تغییر ساعت:",
        "• <code>حالت شب شروع ۲۳:۳۰</code>",
        "• <code>حالت شب پایان ۰۷:۰۰</code>",
    ])
    keyboard = markup([
        [toggle_button("🌙 حالت شب", "night_mode_enabled", bool(settings.get("night_mode_enabled")), panel)],
        [btn("🎯 اقدام:", cb("noop"))],
        *action_selector(panel, "night_action", action,
                         options={"mute": "سکوت", "delete": "حذف پیام", "kick": "اخراج",
                                  "ban": "بن", "none": "فقط هشدار"}),
        [primary("⏰ راهنمای تنظیم ساعت", cb("cmd", "nighthelp"))],
        reset_button("night", "♻️ بازنشانی حالت شب"),
        row(home()),
    ])
    return text, keyboard


def _panel_messages(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    text_lines = [
        "📌 <b>مدیریت پیام‌ها</b>",
        "",
        "دستورها:",
        "• <code>پین</code> (با ریپلای) • <code>پین متن دلخواه</code>",
        "• <code>حذف پین</code> • <code>پین فعلی</code> • <code>حذف همه پین‌ها</code>",
        "• <code>پاکسازی ۱۰۰</code> • <code>پاکسازی</code> (از پیام ریپلایی تا حالا)",
        "• <code>قوانین</code> • <code>تنظیم قوانین متن...</code> • <code>حذف قوانین</code>",
    ]
    rows = [
        [primary("📌 پین", cb("cmd", "pin")), primary("📍 حذف پین", cb("cmd", "unpin")),
         primary("📌 پین فعلی", cb("cmd", "getpin"))],
        [primary("📜 مشاهده قوانین", cb("cmd", "rules")), primary("✏️ ویرایش قوانین", cb("cmd", "setrules"))],
        [primary("🧹 پاکسازی ۱۰۰", cb("cmd", "purge100")), danger("🧹 حذف همه پین‌ها", cb("conf", "unpinall", "all"))],
        [primary("📜 قوانین", cb("panel", "rules"))],
    ]
    text_lines.extend([
        "",
        "⏱ همهٔ پیام‌های عادی ربات در گروه پس از ۳۰ ثانیه پاک می‌شوند؛ پنل مدیریتی باقی می‌ماند.",
        "گزارش‌های خصوصیِ بن در پی‌وی مالک و روت‌های مجاز باقی می‌مانند.",
    ])
    rows.append(row(home()))
    return "\n".join(text_lines), markup(rows)


def _panel_rules(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    rules = (settings.get("rules_text") or "").strip()
    preview = rules[:400] + ("…" if len(rules) > 400 else "")
    text = "\n".join([
        "📜 <b>قوانین گروه</b>",
        "",
        preview or "قوانینی تنظیم نشده است.",
        "",
        "• <code>تنظیم قوانین متن قوانین...</code>",
        "• <code>حذف قوانین</code>",
    ])
    keyboard = markup([
        [primary("📜 مشاهده قوانین", cb("cmd", "rules"))],
        [primary("✏️ ویرایش قوانین", cb("cmd", "setrules")), danger("🗑 حذف قوانین", cb("conf", "delrules", "all"))],
        reset_button("rules", "♻️ بازگشت قوانین به پیش‌فرض"),
        row(home()),
    ])
    return text, keyboard


def _panel_stats(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join([
        "📊 <b>آمار و فعالیت</b>",
        "",
        "دستورها:",
        "• <code>آمار</code> — آمار کلی گروه",
        "• <code>فعال‌ترین‌ها</code> / <code>فعال‌ترین‌ها ۳۰</code>",
        "• <code>غیرفعال‌ها ۳۰</code>",
        "• <code>روند گروه</code> — نمودار رشد اعضا",
        "",
        "ℹ️ آمار فقط شامل پیام‌هایی است که ربات دریافت کرده است.",
    ])
    keyboard = markup([
        [primary("📊 آمار گروه", cb("cmd", "stats")), primary("🏆 فعال‌ترین‌ها", cb("cmd", "top"))],
        [primary("💤 غیرفعال‌ها", cb("cmd", "inactive")), primary("📈 روند گروه", cb("cmd", "trend"))],
        row(home()),
    ])
    return text, keyboard


# --------------------------------------------------------------------------- #
# Telegram administrator rights editor
# --------------------------------------------------------------------------- #
# (flag, Persian label, default in the "basic moderator" preset)
ADMIN_RIGHTS: tuple[tuple[str, str, bool], ...] = (
    ("can_delete_messages", "🗑 حذف پیام", True),
    ("can_restrict_members", "🚫 بن و محدود کردن", True),
    ("can_invite_users", "🔗 دعوت کاربر", True),
    ("can_pin_messages", "📌 سنجاق پیام", True),
    ("can_change_info", "✏️ تغییر اطلاعات گروه", False),
    ("can_promote_members", "➕ افزودن مدیر جدید", False),
    ("can_manage_video_chats", "🎥 چت صوتی/تصویری", False),
    ("is_anonymous", "🕶 ارسال ناشناس", False),
)

ADMIN_RIGHT_KEYS = tuple(key for key, _, _ in ADMIN_RIGHTS)
RIGHT_PRESETS = {
    "full": {key: True for key in ADMIN_RIGHT_KEYS},
    "basic": {key: default for key, _, default in ADMIN_RIGHTS},
    "none": {key: False for key in ADMIN_RIGHT_KEYS},
}


def admin_rights_lines(name: str, user_id: int, rights: dict) -> list[str]:
    lines = ["🎚 <b>دسترسی‌های مدیر</b>", "",
             f"👤 {name or user_id} — <code>{user_id}</code>", "",
             "روی هر مورد بزنید تا فعال یا غیرفعال شود:"]
    for key, label, _default in ADMIN_RIGHTS:
        mark = "✅" if rights.get(key) else "❌"
        lines.append(f"{mark} {label}")
    return lines


def admin_rights_keyboard(user_id: int, rights: dict) -> InlineKeyboardMarkup:
    rows: list[list] = []
    current: list = []
    for key, label, _default in ADMIN_RIGHTS:
        mark = "✅" if rights.get(key) else "❌"
        current.append(primary(f"{mark} {label}", cb("ap", "t", user_id, key)))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([success("👑 مدیر کامل", cb("ap", "full", user_id)),
                 primary("🛡 دسترسی پایه", cb("ap", "basic", user_id))])
    rows.append([primary("🔄 به‌روزرسانی", cb("ap", "r", user_id)),
                 danger("⬇️ عزل مدیر", cb("ap", "demote", user_id))])
    rows.append(row(primary("❌ بستن", cb("ap", "x", user_id))))
    return markup(rows)


def _panel_members(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    counts = ctx.get("member_counts") or {}
    admins = counts.get("admins")
    admin_count_text = (to_persian_digits(str(admins)) if admins is not None
                        else "نامشخص")
    text = "\n".join([
        "👥 <b>اعضا و مدیران</b>",
        "",
        f"👮 مدیران تلگرام: {admin_count_text}",
        f"🛡 کادر ربات: {to_persian_digits(str(counts.get('staff', 0)))}",
        f"⭐️ کاربران ویژه: {to_persian_digits(str(counts.get('trusted', 0)))}",
        f"🏷 تگ‌دارها: {to_persian_digits(str(counts.get('tags', 0)))}",
        "",
        "دستورها: <code>لیست مدیران</code> • <code>ارتقا</code> • <code>تنزل</code>",
        "",
        "🎚 برای ویرایش دسترسی‌های یک مدیر تلگرام، روی پیام او ریپلای کنید و",
        "بنویسید <code>دسترسی مدیر</code>.",
    ])
    keyboard = markup([
        [primary("📋 لیست مدیران", cb("cmd", "admins")), primary("⭐️ کاربران ویژه", cb("cmd", "trustedlist"))],
        [primary("🏷 تگ‌ها", cb("panel", "tags")), primary("💤 غیرفعال‌ها", cb("cmd", "inactive"))],
        [primary("👮 نقش‌ها", cb("panel", "staff")), primary("🎚 دسترسی مدیر", cb("cmd", "adminperm"))],
        row(home()),
    ])
    return text, keyboard


def _panel_fun(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join([
        "🎮 <b>سرگرمی و کاربردی</b>",
        "",
        "• <code>جوک</code> • <code>فال حافظ</code> • <code>شیر یا خط</code>",
        "• <code>تاس</code> • <code>عدد تصادفی ۱ ۱۰۰</code> • <code>اسلات</code> • <code>بازی</code>",
        "• <code>ارز</code> • <code>قیمت دلار</code> • <code>قیمت بیت‌کوین</code> • <code>طلا</code> • <code>بورس</code>",
        "• <code>تاریخ</code> • <code>ساعت</code>",
    ])
    keyboard = markup([
        [primary("😂 جوک", cb("cmd", "joke")), primary("🔮 فال حافظ", cb("cmd", "fal"))],
        [primary("🪙 شیر یا خط", cb("cmd", "coin")), primary("🎲 تاس", cb("cmd", "dice")),
         primary("🎰 اسلات", cb("cmd", "slot"))],
        [primary("🔢 عدد تصادفی", cb("cmd", "rand")), primary("🎮 بازی حدس عدد", cb("cmd", "game"))],
        [primary("💱 نرخ ارز", cb("cmd", "currency")), primary("🪙 قیمت ارز دیجیتال", cb("cmd", "crypto"))],
        [primary("🥇 طلا و سکه", cb("cmd", "gold")), primary("📈 شاخص بورس", cb("cmd", "bourse"))],
        [primary("📅 تاریخ", cb("cmd", "date")), primary("🕒 ساعت", cb("cmd", "time"))],
        row(home()),
    ])
    return text, keyboard


def _panel_ai_chat(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    import html
    import math

    from ..services.ai_gateway import model_ref

    settings = ctx.get("settings") or {}
    selected = str(settings.get("ai_chat_model") or "")
    models = list(ctx.get("ai_models") or [])
    error = ctx.get("ai_models_error")
    page_size = 6
    page_count = max(1, math.ceil(len(models) / page_size))
    try:
        page = max(0, min(int(ctx.get("ai_model_page", 0)), page_count - 1))
    except (TypeError, ValueError):
        page = 0

    current_model = next((model for model in models if model.id == selected), None)
    lines = ["💬 <b>گفت‌وگوی هوشمند گروه/کانال</b>", ""]
    if selected:
        current_name = current_model.name if current_model else selected
        lines.extend([
            f"مدل فعلی: <b>{html.escape(str(current_name))}</b>",
            f"شناسهٔ مدل: <code>{html.escape(selected)}</code>",
        ])
    else:
        lines.append("مدلی برای این گروه انتخاب نشده است.")

    lines.extend([
        "",
        "شروع: در گروه یا کانال پیام را با «آرماندو» آغاز کنید؛ نمونه: <code>آرماندو خلاصهٔ گفتگو را بگو</code>",
        "ادامه: روی پاسخ آرماندو ریپلای کنید و پرسش بعدی را بنویسید؛ گفت‌وگو در همان چت مشترک است.",
        "مدیران برای هر گروه/کانال، مدل را جداگانه از فهرست زندهٔ API انتخاب می‌کنند.",
        state_text("حافظهٔ ۷روزهٔ گفتگو", bool(settings.get("ai_agent_enabled"))),
        "با فعال‌بودن حافظه، متن و کپشن پیام‌هایی که تلگرام به ربات می‌رساند تا ۷ روز نگهداری و سپس خودکار پاک می‌شوند.",
        "آرماندو فقط می‌خواند و در پاسخ به درخواست، پاسخ می‌سازد؛ بن، سکوت، حذف پیام یا تغییر تنظیمات انجام نمی‌دهد. پیام‌های پیش از افزودن ربات یا خارج از دسترسی تلگرام دیده نمی‌شوند.",
        "⚠️ رسانه‌ها به‌صورت نوع پیام/کپشن در حافظه می‌آیند؛ صدای ویس رونویسی نمی‌شود.",
    ])

    if error == "not_configured":
        lines.extend(["", "⛔️ مدیر ربات باید <code>AI_CHAT_BASE_URL</code> و <code>AI_CHAT_API_KEY</code> را در Railway تنظیم کند؛ مقدار کلید را Secret بگذارد."])
    elif error:
        lines.extend(["", "⚠️ فهرست مدل‌ها از ارائه‌دهندهٔ API دریافت نشد. کلید و دسترسی API را در Railway بررسی و سپس تازه‌سازی کنید."])
    elif models:
        lines.extend(["", f"<b>مدل‌های در دسترس ({len(models)}):</b>"])
        start = page * page_size
        for model in models[start:start + page_size]:
            marker = "✅" if model.id == selected else "•"
            lines.append(
                f"{marker} <b>{html.escape(str(model.name)[:80])}</b>\n"
                f"   <code>{html.escape(str(model.id)[:180])}</code>"
            )
        lines.append(f"صفحهٔ {to_persian_digits(str(page + 1))} از {to_persian_digits(str(page_count))}")
    else:
        lines.extend(["", "در فهرست زنده مدلی برای نمایش نیست."])

    rows: list[list[Any]] = []
    if models:
        start = page * page_size
        model_buttons = []
        for model in models[start:start + page_size]:
            name = str(model.name).strip() or model.id
            if len(name) > 34:
                name = name[:33].rstrip() + "…"
            prefix = "✅ " if model.id == selected else "• "
            model_buttons.append(primary(prefix + name, cb("aimodel", "pick", model_ref(model.id))))
        rows.extend(grid(model_buttons, per_row=1))
        pagination = []
        if page > 0:
            pagination.append(primary("⬅️ مدل‌های قبلی", cb("aimodel", "page", page - 1)))
        if page + 1 < page_count:
            pagination.append(primary("مدل‌های بعدی ➡️", cb("aimodel", "page", page + 1)))
        if pagination:
            rows.append(pagination)

    rows.append([primary("🔄 تازه‌سازی فهرست مدل‌ها", cb("aimodel", "refresh"))])
    if selected:
        rows.append([toggle_button("🧠 حافظهٔ گفتگو (۷ روز)", "ai_agent_enabled",
                                   bool(settings.get("ai_agent_enabled")), "ai_chat")])
        rows.append([danger("🧹 پاک‌کردن حافظهٔ این چت", cb("aimemory", "ask_clear"))])
        rows.append([danger("⏹ خاموش کردن گفت‌وگوی گروه", cb("aimodel", "off"))])
    rows.append([danger("♻️ بازگشت AI به پیش‌فرض", cb("reset", "ask", "ai"))])
    rows.append([primary("⬅️ بازگشت به تنظیمات", cb("panel", "settings")), home()])
    return "\n".join(lines), markup(rows)


def _panel_settings(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    level = ctx.get("level", 0)
    text = "\n".join([
        "⚙️ <b>تنظیمات گروه</b>",
        "",
        "هر بخش را انتخاب کنید تا وضعیت و تنظیمات آن را ببینید.",
    ])
    buttons = []
    for key in SETTINGS_ORDER:
        if key not in SECTIONS:
            continue
        label, minimum_role, _ = SECTIONS[key]
        if level < level_of(minimum_role):
            continue
        buttons.append(primary(label, cb("panel", key)))
    buttons.append(primary("📢 گزارش‌ها", cb("panel", "reports")))
    rows = grid(buttons, per_row=2)
    rows.append([primary("💾 پشتیبان‌گیری", cb("panel", "backup"))])
    rows.append([primary("♻️ بازنشانی بخش‌به‌بخش", cb("resetmenu", "open"))])
    rows.append(row(home()))
    return text, markup(rows)


def _panel_reports(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    counts = ctx.get("report_counts") or {"open": 0, "total": 0}
    text = "\n".join([
        "📢 <b>گزارش‌ها</b>",
        "",
        state_text("دریافت گزارش", bool(settings.get("report_enabled"))),
        state_text("اطلاع‌رسانی به مدیران", bool(settings.get("report_notify_admins"))),
        f"📨 گزارش‌های باز: {to_persian_digits(str(counts.get('open', 0)))}",
        f"📚 مجموع گزارش‌ها: {to_persian_digits(str(counts.get('total', 0)))}",
        "",
        "اعضا می‌توانند با <code>گزارش</code> (با ریپلای) پیام را گزارش کنند.",
    ])
    keyboard = markup([
        [toggle_button("📢 دریافت گزارش", "report_enabled", bool(settings.get("report_enabled")), "reports"),
         toggle_button("🔔 اطلاع به مدیران", "report_notify_admins",
                       bool(settings.get("report_notify_admins")), "reports")],
        [primary("📋 گزارش‌های باز", cb("cmd", "openreports"))],
        reset_button("reports", "♻️ بازنشانی تنظیمات گزارش"),
        row(home()),
    ])
    return text, keyboard


def _panel_staff(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    staff = ctx.get("staff") or []
    lines = ["👮 <b>نقش‌های داخلی ربات</b>", ""]
    if staff:
        for user_id, role, name in staff[:20]:
            lines.append(f"• {name} — {ROLE_LABELS.get(role, role)}")
    else:
        lines.append("هیچ نقشی ثبت نشده است.")
    lines += ["",
              "دستورها:",
              "• <code>افزودن مدیر</code> • <code>عزل مدیر</code> • <code>لیست مدیران</code>",
              "• <code>افزودن ناظر</code> • <code>افزودن پاکساز</code> • <code>افزودن کمک‌یار</code>"
              ]
    keyboard = markup([
        [primary("📋 لیست مدیران", cb("cmd", "admins")), primary("📋 لیست کادر ربات", cb("cmd", "stafflist"))],
        row(home()),
    ])
    return "\n".join(lines), keyboard


def _panel_trust(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    trust = ctx.get("trusted") or []
    bypass = list(ctx["settings"].get("trust_bypass") or [])
    lines = ["⭐️ <b>کاربران ویژه</b>", ""]
    lines.append(f"تعداد: {to_persian_digits(str(len(trust)))}")
    if trust:
        for user_id, name in trust[:20]:
            lines.append(f"• {name}")
    lines += ["", "معافیت‌های فعال: " + (", ".join(bypass) if bypass else "هیچ‌کدام")]
    keyboard = markup([
        [primary("📋 لیست ویژه‌ها", cb("cmd", "trustedlist")), primary("➕ افزودن ویژه", cb("cmd", "addtrusted"))],
        [primary("➖ عزل ویژه", cb("cmd", "rmtrusted"))],
        reset_button("trust", "♻️ بازنشانی تنظیمات ویژه‌ها"),
        row(home()),
    ])
    return "\n".join(lines), keyboard


def _panel_tags(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    tags = ctx.get("tags") or []
    lines = ["🏷 <b>تگ اعضا</b>", "", f"تعداد: {to_persian_digits(str(len(tags)))}"]
    for user_id, name, tag in tags[:20]:
        lines.append(f"• {name} — <code>{tag}</code>")
    lines += ["",
              "دستورها:",
              "• <code>تگ متن تگ</code> (با ریپلای روی پیام کاربر)",
              "• <code>حذف تگ</code> فقط تگ نمایشی را پاک می‌کند.",
              "• <code>حذف از تگ</code> استثنای جداگانه برای «تگ همه» است؛ با ریپلای، خود مدیر و عضو هدف مستثنا می‌شوند.",
              "• <code>افزودن به تگ</code> استثنا را برمی‌دارد؛ برای خودتان: <code>افزودن به تگ خود</code>.",
              "• <code>لیست تگ‌ها</code> • <code>تگ من متن تگ</code> (اگر توسط مدیر فعال شود)", "",
              "ℹ️ برای درج تگ در «لیست اعضای تلگرام» ربات باید دسترسی "
              "«مدیریت تگ اعضا» داشته باشد."]
    keyboard = markup([
        [primary("📋 لیست تگ‌ها", cb("cmd", "taglist")), primary("🏷 تغییر تگ", cb("cmd", "settag"))],
        [primary("🗑 حذف تگ نمایشی", cb("cmd", "deltag"))],
        [primary("🚫 حذف از تگ همه", cb("cmd", "tagexclude")),
         success("✅ افزودن به تگ همه", cb("cmd", "tagrestore"))],
        reset_button("tags", "♻️ بازنشانی تنظیمات تگ"),
        row(home()),
    ])
    return "\n".join(lines), keyboard


def _panel_logs(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = ctx["settings"]
    log_chat = settings.get("log_chat_id")
    text = "\n".join([
        "📜 <b>لاگ و ممیزی</b>",
        "",
        state_text("ثبت لاگ", bool(settings.get("log_enabled"))),
        f"📨 کانال لاگ: {log_chat or 'تنظیم نشده'}",
        "",
        "برای تنظیم: <code>تنظیم لاگ</code> (در کانال یا گروه لاگ ریپلای نکنید؛ فقط دستور را بفرستید)",
        "همچنین: <code>تاریخچه</code> (با ریپلای) برای مشاهده سوابق کاربر",
    ])
    keyboard = markup([
        [toggle_button("📜 ثبت لاگ", "log_enabled", bool(settings.get("log_enabled")), "logs")],
        [primary("⚙️ تنظیم کانال لاگ", cb("cmd", "setlog"))],
        [primary("📜 تاریخچه کاربر", cb("cmd", "history"))],
        reset_button("logs", "♻️ بازنشانی تنظیمات لاگ"),
        row(home()),
    ])
    return text, keyboard


def _panel_schedule(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    items = ctx.get("scheduled") or []
    lines = ["⏰ <b>پیام‌های زمان‌بندی‌شده</b>", ""]
    if items:
        for row_item in items[:15]:
            lines.append(f"• {row_item}")
    else:
        lines.append("هیچ پیام زمان‌بندی‌شده‌ای وجود ندارد.")
    lines += ["",
              "دستورها:",
              "• <code>زمان‌بندی ۲۴ساعت متن پیام</code>",
              "• <code>زمان‌بندی روزانه ۰۹:۰۰ متن پیام</code>",
              "• <code>لیست زمان‌بندی</code> • <code>حذف زمان‌بندی ۱</code>"]
    keyboard = markup([
        [primary("📋 لیست", cb("cmd", "listschedule")), primary("➕ افزودن", cb("cmd", "addschedule"))],
        [danger("🗑 حذف همه", cb("conf", "clearschedule", "all"))],
        row(home()),
    ])
    return "\n".join(lines), keyboard


def _panel_federation(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    fed = ctx.get("federation")
    lines = ["🌐 <b>فدراسیون</b>", ""]
    if fed:
        lines.append(f"📛 نام: <code>{fed.get('name')}</code>")
        lines.append(f"👮 مدیران: {to_persian_digits(str(fed.get('admins', 0)))}")
        lines.append(f"💬 گروه‌ها: {to_persian_digits(str(fed.get('chats', 0)))}")
        lines.append(f"🚫 بن‌ها: {to_persian_digits(str(fed.get('bans', 0)))}")
        lines.append(f"⚙️ حالت: {fed.get('mode', 'اشتراک‌گذاری')}")
    else:
        lines.append("این گروه به هیچ فدراسیونی متصل نیست.")
    lines += ["", "دستورها: <code>ساخت فدراسیون نام</code> • <code>اتحاد فدراسیون نام</code> • <code>ترک فدراسیون</code>"]
    keyboard = markup([
        [primary("📋 لیست بن‌ها", cb("cmd", "fbanlist")), primary("ℹ️ اطلاعات", cb("cmd", "fedinfo"))],
        [primary("➕ ساخت", cb("cmd", "fedcreate")), primary("🔗 اتصال", cb("cmd", "fedjoin"))],
        [danger("🚪 ترک فدراسیون", cb("conf", "fedleave", "all"))],
        row(home()),
    ])
    return "\n".join(lines), keyboard


def _panel_backup(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join([
        "💾 <b>پشتیبان‌گیری و بازیابی</b>",
        "",
        "• <code>پشتیبان‌گیری</code> — دریافت فایل تنظیمات گروه",
        "• <code>بازیابی</code> — ارسال فایل پشتیبان (با ریپلای روی فایل)",
        "",
        "ℹ️ فایل شامل قفل‌ها، فیلترها، یادداشت‌ها، اخطارها، نقش‌ها و تنظیمات است.",
        "ℹ️ هیچ اطلاعات محرمانه‌ای (توکن و کلیدها) در فایل ذخیره نمی‌شود.",
    ])
    keyboard = markup([
        [success("💾 گرفتن پشتیبان", cb("cmd", "backup"))],
        [primary("♻️ راهنمای بازیابی", cb("cmd", "restorehelp"))],
        row(home()),
    ])
    return text, keyboard


def _panel_disabled(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    from ..services.disabled import DISABLEABLE

    settings = ctx["settings"]
    disabled = list(settings.get("disabled_commands") or [])
    lines = ["🚫 <b>بخش‌های غیرفعال</b>", ""]
    for key, label in DISABLEABLE.items():
        lines.append(f"{label}: {state_icon(key not in disabled)}")
    buttons = [onoff(f"{label}  {state_icon(key not in disabled)}", key not in disabled,
                     cb("dis", key)) for key, label in DISABLEABLE.items()]
    rows = grid(buttons, per_row=2)
    rows.append(reset_button("disabled", "♻️ فعال‌سازی دوبارهٔ همهٔ بخش‌ها"))
    rows.append(row(home()))
    return "\n".join(lines), markup(rows)


def _panel_help(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    level = ctx.get("level", 0)
    text = "\n".join([
        "📚 <b>راهنمای ربات</b>",
        "",
        f"🤖 {BOT_NAME}",
        "",
        "دستورها به صورت متن ساده و فارسی هستند.",
        "برای اجرا روی یک کاربر، روی پیام او <b>ریپلای</b> کنید.",
        "",
        "مثال‌ها:",
        "• <code>بن ۲روز تبلیغ</code>",
        "• <code>سکوت ۳۰دقیقه</code>",
        "• <code>اخطار رعایت نکردن قوانین</code>",
        "• <code>تعداد اخطار ۴</code>",
        "• <code>قفل لینک</code> / <code>بازکردن لینک</code>",
        "• <code>فیلتر کردن کلمه</code> / <code>لیست فیلتر</code>",
        "• <code>ذخیره قوانین = متن</code> / <code>گرفتن قوانین</code>",
        "• <code>پاکسازی ۱۰۰</code> • <code>آمار</code> • <code>لیست مدیران</code>",
    ])
    keyboard = markup([
        [primary("🛡 راهنمای مدیریت", cb("help", "mod")), primary("🔒 راهنمای قفل‌ها", cb("help", "locks"))],
        [primary("📝 راهنمای فیلتر و یادداشت", cb("help", "filters")),
         primary("👋 راهنمای خوشامد", cb("help", "welcome"))],
        [primary("🎮 راهنمای سرگرمی", cb("help", "fun")), primary("💱 راهنمای ارز", cb("help", "market"))],
        row(home()),
    ])
    if level >= level_of("admin"):
        keyboard = markup([
            [primary("🛡 مدیریت", cb("help", "mod")), primary("🔒 قفل‌ها", cb("help", "locks"))],
            [primary("📝 فیلترها", cb("help", "filters")), primary("👋 خوشامد", cb("help", "welcome"))],
            [primary("🎮 سرگرمی", cb("help", "fun")), primary("💱 ارز", cb("help", "market"))],
            [primary("⚙️ تنظیمات", cb("panel", "settings"))],
            row(home()),
        ])
    return text, keyboard


def _panel_profile(ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    profile = ctx.get("profile") or {}
    lines = ["👤 <b>پنل اختصاصی من</b>", ""]
    lines.append(f"🙋 نام: {profile.get('name', '—')}")
    if profile.get("username"):
        lines.append(f"🆔 نام کاربری: @{profile['username']}")
    lines.append(f"🔢 شناسه: <code>{profile.get('user_id', '—')}</code>")
    lines.append(f"🎖 سمت: {profile.get('role', '👤 عضو')}")
    if profile.get("tag"):
        lines.append(f"🏷 تگ: <code>{profile['tag']}</code>")
    lines.append(f"📨 پیام‌های من: {to_persian_digits(str(profile.get('messages', 0)))}")
    lines.append(f"⚠️ اخطارهای من: {to_persian_digits(str(profile.get('warns', 0)))}")
    keyboard = markup([
        [primary("ℹ️ اطلاعات من", cb("cmd", "me"))],
        [primary("📜 قوانین گروه", cb("cmd", "rules")), primary("📊 آمار گروه", cb("cmd", "stats"))],
        [primary("💤 وضعیت AFK", cb("cmd", "afkhelp")), primary("📚 راهنما", cb("panel", "help"))],
        row(home()),
    ])
    return "\n".join(lines), keyboard


PANEL_BUILDERS: dict[str, Callable[[dict], tuple[str, InlineKeyboardMarkup]]] = {
    "main": lambda ctx: (main_menu_text(ctx.get("chat_title", ""), ctx.get("role_label", "")),
                         main_menu(ctx.get("level", 0), chat_type=ctx.get("chat_type", "group"))),
    "users": _panel_users,
    "security": _panel_security,
    "locks": _panel_locks,
    "lock": _panel_lock_detail,
    "filters": _panel_filters,
    "welcome": _panel_welcome,
    "messages": _panel_messages,
    "stats": _panel_stats,
    "members": _panel_members,
    "fun": _panel_fun,
    "settings": _panel_settings,
    "ai_chat": _panel_ai_chat,
    "help": _panel_help,
    "profile": _panel_profile,
    "warnings": _panel_warnings,
    "captcha": _panel_captcha,
    "antispam": _panel_antispam,
    "night": _panel_night,
    "raid": _panel_raid,
    "staff": _panel_staff,
    "trust": _panel_trust,
    "logs": _panel_logs,
    "schedule": _panel_schedule,
    "federation": _panel_federation,
    "backup": _panel_backup,
    "disabled": _panel_disabled,
    "tags": _panel_tags,
    "rules": _panel_rules,
    "reports": _panel_reports,
}


def build_panel(section: str, ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    builder = PANEL_BUILDERS.get(section)
    if builder is None:
        return main_menu_text(ctx.get("chat_title", ""), ctx.get("role_label", "")), \
            main_menu(ctx.get("level", 0), chat_type=ctx.get("chat_type", "group"))
    return builder(ctx)


def help_text(topic: str) -> str:
    topics = {
        "mod": "\n".join([
            "🛡 <b>مدیریت کاربران</b>", "",
            "• روی پیام هدف ریپلای کنید، یا شناسه/نام‌کاربری مشخص بدهید: <code>بن @username</code> / <code>بن @username ۲روز تبلیغ</code>",
            "• <code>رفع بن @username</code> • <code>کیک @username</code> • <code>سکوت @username ۳۰دقیقه</code>",
            "• با ریپلای می‌توانید دلیل کوتاه هم بنویسید: <code>اخطار تبلیغ</code> / <code>بن تبلیغ</code>",
            "• <code>لغو سکوت</code> / <code>کسر اخطار</code> / <code>صفر کردن اخطار</code>",
            "• فقط فرمان دقیق با هدف معتبر اجرا می‌شود؛ جمله‌هایی مثل «بن ات می‌کنم» فرمان نیستند.",
            "• <code>تعداد اخطار ۴</code> / <code>وضعیت اخطار</code> / <code>تاریخچه</code>",
            "• <code>لیست بن</code> / <code>لیست سکوت</code> / <code>اطلاعات کاربر</code>",
            "• مولتی‌بن: روی فهرستِ هر خط یک شناسهٔ عددی یا <code>@username</code> ریپلای کنید و <code>مولتی بن</code> بفرستید؛ پس از پیش‌نمایش، «تأیید» را بزنید یا «لغو» کنید.",
        ]),
        "locks": "\n".join([
            "🔒 <b>قفل‌ها</b>", "",
            "• <code>قفل لینک</code> / <code>بازکردن لینک</code>",
            "• <code>قفل عکس</code> / <code>قفل ویدیو</code> / <code>قفل استیکر</code> / <code>قفل فوروارد</code>",
            "• <code>قفل فحاشی</code> / <code>قفل پورن</code> / <code>قفل انگلیسی</code>",
            "• <code>قفل چینی</code> / <code>قفل روسی</code> / <code>قفل هندی</code>",
            "• <code>قفل طولانی</code> (پیامِ بیش از ۱۰۰۰ کاراکتر → حذف + سکوت)",
            "• <code>قفل ناشناس</code> (ارسال با نام کانال دیگر → حذف/مسدودسازی کانال)",
            "• پیام با نام همین گروه پیش‌فرض از این قفل معاف است؛ سازنده می‌تواند با "
            "<code>استثنای ناشناس خاموش</code> این معافیت را بردارد.",
            "• تلگرام هویت انسانیِ مدیر ناشناس را به ربات نشان نمی‌دهد.",
            "• <code>قفل گروه</code> / <code>قفل گروه رسانه</code> / <code>باز کردن گروه</code>",
            "• <code>قفل خروج</code> (بن خودکار هرکس خارج شود)",
            "• <code>بن خروج سریع ۱۰</code> (بنِ خروجِ بلافاصله پس از ورود)",
            "• هر بن موفق، مستقل از قفل خروج، با جزئیات کامل به پی‌وی مالک بات و دارندگان روت همان گروه می‌رود؛ همهٔ پیام‌های ربات در گروه پس از ۳۰ ثانیه پاک می‌شوند.",
            "• گزینهٔ «گزارش خروج بدون بن» فقط گزارش خصوصیِ خروج‌هایی را کنترل می‌کند که به بن منجر نشده‌اند.",
            "• <code>وضعیت قفل گروه</code>",
            "• برای مشاهده همه قفل‌ها: <code>لیست قفل‌ها</code> یا پنل «🔒 قفل‌ها»",
        ]),
        "filters": "\n".join([
            "📝 <b>فیلترها و یادداشت‌ها</b>", "",
            "• <code>فیلتر کردن کلمه</code> (مسدود کردن کلمه)",
            "• <code>فیلتر سلام = خوش آمدید</code> (پاسخ خودکار)",
            "• <code>حذف فیلتر کلمه</code> / <code>لیست فیلتر</code> / <code>پاکسازی فیلترها</code>",
            "• <code>ذخیره قوانین = متن</code> / <code>گرفتن قوانین</code> / <code>#قوانین</code>",
            "• <code>لیست یادداشت‌ها</code> / <code>حذف قوانین</code>",
        ]),
        "welcome": "\n".join([
            "👋 <b>خوشامد و خداحافظی</b>", "",
            "• <code>تنظیم خوشامد سلام {first_name} عزیز</code>",
            "• <code>تنظیم خداحافظی خداحافظ {first_name}</code>",
            "• <code>خاموش‌کردن خوشامد</code> / <code>روشن‌کردن خوشامد</code>",
            "• <code>تنظیم کپچا</code> / <code>حالت کپچا ریاضی</code>",
            "• مکان‌های قابل استفاده: {first_name} {last_name} {username} {chat_name} {date} {time}",
        ]),
        "fun": "\n".join([
            "🎮 <b>سرگرمی</b>", "",
            "• <code>جوک</code> • <code>فال حافظ</code> • <code>شیر یا خط</code>",
            "• <code>تاس</code> • <code>عدد تصادفی ۱ ۱۰۰</code> • <code>اسلات</code>",
            "• <code>بازی</code> (حدس عدد) — برای حدس زدن عدد را بنویسید",
        ]),
        "rules": "\n".join([
            "📜 <b>قوانین</b>", "",
            "• <code>تنظیم قوانین ۱. احترام ۲. بدون تبلیغ</code>",
            "• <code>قوانین</code> یا <code>#قوانین</code> برای نمایش",
            "• <code>حذف قوانین</code>",
        ]),
        "stats": "\n".join([
            "📊 <b>آمار</b>", "",
            "• <code>آمار</code> — آمار کلی گروه",
            "• <code>فعال‌ترین‌ها ۱۰</code> / <code>غیرفعال‌ها ۳۰</code>",
            "• <code>آمار من</code>",
            "• ℹ️ آمار از زمان حضور ربات جمع‌آوری می‌شود.",
        ]),
        "messages": "\n".join([
            "📌 <b>پیام‌ها</b>", "",
            "• <code>پین</code> (با ریپلای) / <code>لغو پین</code> / <code>لغو همه پین‌ها</code>",
            "• <code>پاکسازی ۱۰۰</code> / <code>سنجاق لیست</code>",
            "• <code>زمان‌بندی ۲۴ساعت متن پیام</code> / <code>لیست زمان‌بندی</code>",
            "• <code>حذف زمان‌بندی ۱</code> — حذف زمان‌بندی با شناسهٔ فهرست",
        ]),
        "members": "\n".join([
            "👥 <b>اعضا</b>", "",
            "• <code>اطلاعات کاربر</code> (با ریپلای)",
            "• <code>تگ مدیر فروش</code> / <code>حذف تگ</code> / <code>لیست تگ‌ها</code>",
            "• <code>افزودن ناظر</code> / <code>عزل ناظر</code> (با ریپلای)",
            "• <code>لیست مدیران</code> / <code>معافیت‌ها</code>",
            "• <code>تگ همه</code> (منشن اعضای شناخته‌شده در بسته‌های ۵نفره؛ با ریپلای به همان پیام)",
            "• <code>حذف از تگ</code> (استثنای جدا از تگ نمایشی؛ با ریپلای، خود مدیر و عضو هدف مستثنا می‌شوند)",
            "• <code>افزودن به تگ</code> برای برداشتن استثنا؛ برای خودتان <code>افزودن به تگ خود</code>",
        ]),
        "notes": "\n".join([
            "🗒 <b>یادداشت‌ها و دستورات شخصی</b>", "",
            "• <code>ذخیره قوانین = متن</code> / <code>گرفتن قوانین</code>",
            "• <code>لیست یادداشت‌ها</code> / <code>حذف قوانین</code>",
            "• <code>افزودن دستور قیمت = لیست قیمت‌ها</code>",
        ]),
        "report": "\n".join([
            "📢 <b>گزارش</b>", "",
            "• <code>گزارش</code> (با ریپلای روی پیام خاطی)",
            "• <code>گزارش دلیل متخلف</code> — ثبت گزارش با دلیل",
        ]),
        "federation": "\n".join([
            "🌐 <b>فدراسیون</b>", "",
            "• <code>ساخت فدراسیون نام</code> / <code>اتحاد فدراسیون شناسه</code>",
            "• <code>بن فدراسیون</code> / <code>رفع بن فدراسیون</code>",
            "• <code>مدیران فدراسیون</code> / <code>قوانین فدراسیون</code>",
        ]),
        "staff": "\n".join([
            "👮 <b>مدیران و نقش‌ها</b>", "",
            "• <code>ارتقا</code> / <code>تنزل</code> (مدیر تلگرام - با ریپلای)",
            "• <code>دسترسی مدیر</code> (ویرایش دسترسی‌های یک مدیر - با ریپلای)",
            "• <code>ارتقا کامل</code> (ارتقا با همهٔ دسترسی‌ها)",
            "• <code>افزودن ناظر</code> / <code>افزودن مدیر</code> / <code>عزل ناظر</code>",
            "• <code>لیست مدیران</code> / <code>معافیت‌ها</code>",
            "• <code>تگ همه</code> (منشن اعضای شناخته‌شده در بسته‌های ۵نفره؛ با ریپلای به همان پیام)",
        ]),
        "tools": "\n".join([
            "🛠 <b>ابزارها</b>", "",
            "• <code>ساخت لینک</code> / <code>لینک گروه</code>",
            "• <code>تنظیم عنوان نام جدید</code> / <code>تنظیم توضیحات متن</code>",
            "• <code>بکاپ</code> / <code>آیدی</code> / <code>تاریخ</code> / <code>ساعت</code>",
            "• ارسال پیوند مستقلِ پست/ریل عمومی اینستاگرام برای دریافت رسانه.",
            "• <code>آرماندو پرسش</code> — گفت‌وگوی هوشمند گروهی؛ ادامه با ریپلای به پاسخ آرماندو.",
        ]),
        "owner": "\n".join([
            "🌍 <b>دستورهای مالک ربات</b>", "",
            "• <code>ارسال همگانی</code> در خصوصی ربات: متن را در گیومه بفرستید یا روی پیام ریپلای کنید، مقصدهای فعالِ شناخته‌شده را انتخاب کنید و پیش از ارسال مرور/تأیید کنید.",
            "• رابط برنامه‌نویسی ربات تلگرام (Bot API) فهرست کامل چت‌ها را نمی‌دهد؛ انتخاب فقط از چت‌های فعالِ ثبت‌شده در پایگاه داده است.",
            "• <code>دسترسی روت @username</code> در گروه: فقط اگر همان کاربر همین‌جا مدیر/مالک است؛ در خصوصی، چت مجاز را انتخاب کنید.",
            "• <code>لغو دسترسی روت @username</code> در گروه فقط همان چت را لغو می‌کند؛ در خصوصی همهٔ واگذاری‌های همان کاربر لغو می‌شود.",
            "• <code>فهرست دسترسی‌های روت</code> فقط در خصوصی نمایش داده می‌شود.",
            "• روت فقط در چت واگذارشده و پس از بررسی مجدد مدیر بودن می‌تواند ارسال همگانی کند؛ هیچ دسترسی یا بن سراسری داده نمی‌شود.",
        ]),
        "market": "\n".join([
            "💱 <b>ارز، طلا و بورس</b>", "",
            "• <code>ارز</code> — نمای کلی بازار",
            "• <code>قیمت دلار</code> / <code>قیمت یورو</code> / <code>قیمت درهم</code>",
            "• <code>قیمت بیت‌کوین</code> / <code>قیمت btc</code> / <code>قیمت تتر</code>",
            "• <code>طلا</code> / <code>سکه</code> / <code>انس</code>",
            "• <code>بورس</code> — شاخص کل",
            "",
        ]),
    }
    return topics.get(topic, "📚 برای مشاهده راهنما یکی از بخش‌ها را انتخاب کنید.")


def confirm_text(action: str) -> str:
    messages = {
        "clearwarns": "⚠️ آیا مطمئن هستید؟ تمام اخطارهای این گروه پاک می‌شود.",
        "clearfilters": "⚠️ آیا مطمئن هستید؟ تمام کلمات فیلتر حذف می‌شوند.",
        "clearnotes": "⚠️ آیا مطمئن هستید؟ تمام یادداشت‌ها حذف می‌شوند.",
        "clearlocks": "⚠️ آیا مطمئن هستید؟ تمام قفل‌ها غیرفعال می‌شوند.",
        "unpinall": "⚠️ آیا مطمئن هستید؟ تمام پیام‌های پین‌شده حذف می‌شوند.",
        "delrules": "⚠️ آیا مطمئن هستید؟ متن قوانین حذف می‌شود.",
        "clearschedule": "⚠️ آیا مطمئن هستید؟ تمام پیام‌های زمان‌بندی‌شده حذف می‌شوند.",
        "fedleave": "⚠️ آیا مطمئن هستید؟ گروه از فدراسیون خارج می‌شود.",
        "ban": "⚠️ آیا از بن کردن این کاربر مطمئن هستید؟",
        "globalban": "⚠️ آیا از بن سراسری این کاربر مطمئن هستید؟",
        "purge": "⚠️ آیا از پاکسازی پیام‌ها مطمئن هستید؟",
    }
    return messages.get(action, "⚠️ آیا مطمئن هستید؟")
