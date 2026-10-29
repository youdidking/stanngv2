"""Time based group protection (night mode)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_delete
from ..core.timeutils import parse_hhmm, tehran_now
from .moderation import PUNISHMENT_OPTIONS_FA, apply_action
from .permissions import Actor, get_bot_actor
from .roles import level_of

logger = logging.getLogger("armando.nightmode")


def window_bounds(start: str, end: str) -> tuple[datetime, datetime] | None:
    """Return the current night window (local time) if it is active now."""
    start_hm = parse_hhmm(start)
    end_hm = parse_hhmm(end)
    if not start_hm or not end_hm:
        return None
    now = tehran_now()
    start_dt = now.replace(hour=start_hm[0], minute=start_hm[1], second=0, microsecond=0)
    end_dt = now.replace(hour=end_hm[0], minute=end_hm[1], second=0, microsecond=0)
    if start_dt == end_dt:
        return None
    if start_dt < end_dt:  # same-day window; the end is exclusive
        return (start_dt, end_dt) if start_dt <= now < end_dt else None
    # window crosses midnight; both intervals also use an exclusive end.
    if now >= start_dt:
        return start_dt, end_dt + timedelta(days=1)
    if now < end_dt:
        return start_dt - timedelta(days=1), end_dt
    return None


def is_night(settings_obj) -> bool:
    if not getattr(settings_obj, "night_mode_enabled", False):
        return False
    return window_bounds(settings_obj.night_start, settings_obj.night_end) is not None


def seconds_until_window_ends(settings_obj) -> int:
    bounds = window_bounds(settings_obj.night_start, settings_obj.night_end)
    if not bounds:
        return 0
    now = tehran_now()
    return max(0, int((bounds[1] - now).total_seconds()))


def bypasses(actor: Actor, settings_obj) -> bool:
    bypass_roles = list(getattr(settings_obj, "night_bypass_roles", None) or [])
    if not bypass_roles:
        return actor.role_level >= level_of("admin") or actor.is_trusted
    for role in bypass_roles:
        if actor.role_level >= level_of(role):
            return True
    return actor.is_trusted and "trusted" in bypass_roles


async def enforce(bot: Bot, session: AsyncSession, *, message, actor: Actor,
                  settings_obj, chat_title: str = "") -> bool:
    """Apply the configured night action and bound restrictions to the window."""
    if not is_night(settings_obj):
        return False
    if bypasses(actor, settings_obj):
        return False

    action = (getattr(settings_obj, "night_action", "mute") or "mute").strip().lower()
    duration = None
    if action in {"mute", "temp_mute"}:
        # The old path passed a duration with action="mute", but apply_action
        # correctly treats that name as permanent. Use the explicit temporary
        # action so the scheduler releases the mute at the window boundary.
        action = "temp_mute"
        duration = max(1, seconds_until_window_ends(settings_obj))
    elif action in {"ban", "temp_ban", "kick"}:
        # Night-only kicks/bans must not strand members after morning.
        action = "temp_ban"
        duration = max(1, seconds_until_window_ends(settings_obj))

    bot_actor = await get_bot_actor(bot, message.chat.id)
    await apply_action(bot, session, chat_id=message.chat.id,
                       target_id=message.from_user.id if message.from_user else message.chat.id,
                       actor=bot_actor, action=action, duration=duration,
                       target_name=message.from_user.full_name if message.from_user else "",
                       message_id=message.message_id if action.startswith("delete") else None,
                       chat_title=chat_title, reason="حالت شب", source="nightmode")
    if action != "none" and not action.startswith("delete"):
        await safe_delete(bot, message.chat.id, message.message_id, context="nightmode_delete")
    return True


def night_status_text(settings_obj) -> str:
    from ..core.normalization import to_persian_digits

    state = "🟢 فعال" if getattr(settings_obj, "night_mode_enabled", False) else "🔴 خاموش"
    active = "در حال اجرا" if is_night(settings_obj) else "خارج از بازه"
    action = getattr(settings_obj, "night_action", "mute") or "mute"
    return "\n".join([
        "🌙 <b>حالت شب</b>",
        "",
        f"وضعیت: {state}",
        f"⏰ بازه: {to_persian_digits(settings_obj.night_start)} تا "
        f"{to_persian_digits(settings_obj.night_end)}",
        f"🎯 اقدام: {PUNISHMENT_OPTIONS_FA.get(action, action)}",
        f"📌 وضعیت فعلی: {active}",
        "🔓 محدودیت‌های موقتِ سکوت/بن/اخراج با پایان بازه آزاد می‌شوند.",
    ])


def suggest_window_text(settings_obj) -> str:
    start_hm = parse_hhmm(settings_obj.night_start) or (1, 0)
    end_hm = parse_hhmm(settings_obj.night_end) or (6, 0)
    from ..core.normalization import to_persian_digits

    return (f"⏰ بازه فعلی: {to_persian_digits(settings_obj.night_start)} "
            f"تا {to_persian_digits(settings_obj.night_end)} "
            f"(ساعت {start_hm[0]:02d}:{start_hm[1]:02d} - {end_hm[0]:02d}:{end_hm[1]:02d})")
