"""Group-wide chat permissions: «قفل گروه» / «باز کردن گروه».

Telegram implements this with ``setChatPermissions``: it changes what the
**ordinary members** of the group may send, while administrators keep every
right.  That is exactly what Persian group-management bots call «قفل گروه».

Three presets are offered; every flag is always transmitted explicitly because
an omitted boolean is read as ``False`` by the Bot API.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import ChatPermissions

from ..core.errors import safe_call

logger = logging.getLogger("armando.chatlock")

# Default (unlocked) group: members may send anything but may not pin or
# rename the group.
MODE_OFF: dict[str, bool] = {
    "can_send_messages": True,
    "can_send_audios": True,
    "can_send_documents": True,
    "can_send_photos": True,
    "can_send_videos": True,
    "can_send_video_notes": True,
    "can_send_voice_notes": True,
    "can_send_polls": True,
    "can_send_other_messages": True,
    "can_add_web_page_previews": True,
    "can_react_to_messages": True,
    "can_edit_tag": True,
    "can_change_info": False,
    "can_invite_users": True,
    "can_pin_messages": False,
    "can_manage_topics": False,
}

# Text is allowed, every media type is blocked.
MODE_MEDIA: dict[str, bool] = {
    **MODE_OFF,
    "can_send_audios": False,
    "can_send_documents": False,
    "can_send_photos": False,
    "can_send_videos": False,
    "can_send_video_notes": False,
    "can_send_voice_notes": False,
    "can_send_polls": False,
    "can_send_other_messages": False,
}

# Only administrators may write.
MODE_ALL: dict[str, bool] = {
    **MODE_OFF,
    "can_send_messages": False,
    "can_send_audios": False,
    "can_send_documents": False,
    "can_send_photos": False,
    "can_send_videos": False,
    "can_send_video_notes": False,
    "can_send_voice_notes": False,
    "can_send_polls": False,
    "can_send_other_messages": False,
    "can_add_web_page_previews": False,
    "can_react_to_messages": False,
    "can_edit_tag": False,
    "can_invite_users": False,
}

MODES: dict[str, dict[str, bool]] = {
    "off": MODE_OFF,
    "media": MODE_MEDIA,
    "all": MODE_ALL,
}

MODE_LABELS: dict[str, str] = {
    "off": "🔓 گروه باز است (همه می‌توانند پیام بفرستند)",
    "media": "🔒 ارسال رسانه قفل است (فقط متن آزاد است)",
    "all": "🔒 گروه قفل است (فقط مدیران می‌توانند پیام بفرستند)",
    "custom": "🎛 وضعیت سفارشی (توسط مدیران تلگرام تغییر کرده است)",
}

MODE_MESSAGES: dict[str, str] = {
    "off": "🔓 گروه باز شد؛ همهٔ اعضا می‌توانند پیام بفرستند.",
    "media": "🔒 ارسال رسانه در گروه قفل شد؛ اعضا فقط می‌توانند متن بفرستند.",
    "all": "🔒 گروه قفل شد؛ از این لحظه فقط مدیران می‌توانند پیام بفرستند.",
}


def permissions_for(mode: str) -> ChatPermissions:
    """The :class:`ChatPermissions` object of a preset (unknown -> unlocked)."""
    return ChatPermissions(**MODES.get(mode, MODE_OFF))


async def apply_chat_lock(bot: Bot, chat_id: int, mode: str) -> bool:
    """Apply one of the presets; returns ``True`` when Telegram accepted it."""
    if mode not in MODES:
        return False
    ok = await safe_call(
        lambda: bot.set_chat_permissions(chat_id=chat_id,
                                         permissions=permissions_for(mode)),
        default=None, context=f"chat_lock:{mode}", log=True)
    return ok is not None


async def chat_lock_state(bot: Bot, chat_id: int) -> str:
    """Return ``off`` / ``media`` / ``all`` / ``custom`` for the chat."""
    chat = await safe_call(lambda: bot.get_chat(chat_id=chat_id), default=None,
                           context="chat_lock_state")
    permissions = getattr(chat, "permissions", None)
    if permissions is None:
        return "off"
    current = {field: bool(getattr(permissions, field, False)) for field in MODE_OFF}
    for mode, values in MODES.items():
        if current == values:
            return mode
    return "custom"
