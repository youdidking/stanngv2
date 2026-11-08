"""Centralized permission model.

Two layers are always combined:

1. **Telegram capabilities** - the real administrator rights of the caller.
2. **Internal roles** - the bot's own hierarchy (:mod:`app.services.roles`).

A permission is granted only when *both* layers agree, therefore the bot can
never be used to escalate privileges.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.types import ChatMember, ChatMemberAdministrator, ChatMemberOwner

from ..config import settings
from ..core import cache
from ..core.normalization import normalize_text
from .roles import Role, level_of

logger = logging.getLogger("armando.permissions")


@dataclass
class Actor:
    """Everything the permission engine needs to know about a caller."""

    user_id: int
    chat_id: int
    chat_type: str = "group"
    telegram_status: str = "member"
    is_anonymous_admin: bool = False
    anonymous_admin_name: str = ""
    bot_role: str | None = None
    is_trusted: bool = False
    trust_bypass: list[str] = field(default_factory=list)
    # ``rights_known`` is False when Telegram could not tell us the member
    # status (network hiccup, anonymous admin, ...).  In that case the bot
    # does NOT block the caller: Telegram itself rejects what is not allowed.
    rights_known: bool = False
    anonymous_admin: bool = False
    # Telegram administrator capabilities
    can_manage_chat: bool = False
    can_delete_messages: bool = False
    can_restrict_members: bool = False
    can_promote_members: bool = False
    can_change_info: bool = False
    can_invite_users: bool = False
    can_pin_messages: bool = False
    can_manage_video_chats: bool = False
    can_post_messages: bool = False
    can_edit_messages: bool = False

    # ------------------------------------------------------------------ helpers
    @property
    def is_bot_owner(self) -> bool:
        return settings.owner_id > 0 and self.user_id == settings.owner_id

    @property
    def is_chat_creator(self) -> bool:
        return self.telegram_status == "creator"

    @property
    def is_group_creator(self) -> bool:
        """True only for Telegram's creator inside a real group chat.

        ``build_actor`` also uses the ``creator`` status for the bot owner in
        private chats; that privilege must never be confused with the owner of
        a Telegram group when enforcing moderation hierarchy.
        """
        return self.chat_type in {"group", "supergroup"} and self.telegram_status == "creator"

    @property
    def is_telegram_admin(self) -> bool:
        return self.telegram_status in {"administrator", "creator"}

    @property
    def role_level(self) -> int:
        """Effective internal level = max(telegram status, internal role)."""
        level = level_of("member")
        if self.telegram_status == "creator":
            level = max(level, int(Role.FOUNDER))
        elif self.telegram_status == "administrator":
            level = max(level, int(Role.ADMIN))
        if self.bot_role:
            level = max(level, level_of(self.bot_role))
        if self.is_bot_owner and level < int(Role.FOUNDER):
            # In private chats the owner always has full access to owner commands.
            # Inside groups the owner is an ordinary user unless explicitly
            # configured otherwise (public bots are used by many groups).
            if self.chat_type == "private" or (
                    settings.owner_founder_in_groups and self.is_telegram_admin):
                level = int(Role.FOUNDER)
        return level

    @property
    def effective_role(self) -> str | None:
        """The role of this actor, Telegram status included.

        A group creator without any internal role is still a ``founder``, so
        role management never fails for the real group owner.
        """
        from .roles import role_for_level

        return role_for_level(self.role_level)

    def has_role(self, minimum: str) -> bool:
        return self.role_level >= level_of(minimum)

    # -------------------------------------------------------------- permissions
    # Design (same family of approaches used by mature group bots):
    #   1. Telegram rights are authoritative - a real admin/creator of the chat
    #      may do everything the *bot* is allowed to do.
    #   2. Internal roles only ADD powers for promoted staff, they never take
    #      powers away from a Telegram admin.
    #   3. When Telegram cannot tell us the rights (rights_known = False) the
    #      call is not blocked: Telegram rejects the action if it is illegal.
    def _allowed(self, minimum: str) -> bool:
        """May this actor run an action that needs the ``minimum`` role?

        * A Telegram creator/administrator may use every command the **bot**
          itself is allowed to perform - Telegram rejects the rest.
        * Promoted staff (internal roles) get exactly what their role grants.
        * The actor's own ``can_*`` flags are deliberately not required: an
          admin that e.g. cannot "add new admins" in Telegram may still ask the
          bot to promote somebody, because the bot acts with its own rights.
        """
        if self.is_telegram_admin:
            return True
        return self.has_role(minimum)

    def can_ban(self) -> bool:
        return self._allowed("moderator")

    def can_kick(self) -> bool:
        return self._allowed("moderator")

    def can_restrict(self) -> bool:
        return self._allowed("muter")

    def can_delete(self) -> bool:
        return self._allowed("cleaner")

    def can_promote(self) -> bool:
        return self._allowed("admin")

    def can_edit_chat_info(self) -> bool:
        return self._allowed("admin")

    def can_pin(self) -> bool:
        return self._allowed("helper")

    def can_invite_via_link(self) -> bool:
        return self._allowed("helper")

    def can_manage_settings(self) -> bool:
        return self._allowed("admin")

    def can_manage_filters(self) -> bool:
        return self._allowed("moderator")

    def can_manage_notes(self) -> bool:
        return self._allowed("helper")

    def can_manage_staff(self) -> bool:
        return self._allowed("admin")

    def can_manage_captcha(self) -> bool:
        return self._allowed("admin")

    def can_use_purge(self) -> bool:
        return self._allowed("cleaner")

    def can_view_logs(self) -> bool:
        return self._allowed("moderator")

    def can_use_connection(self) -> bool:
        return self.is_telegram_admin or self.is_bot_owner

    def bypasses(self, protection: str) -> bool:
        """Whether this actor bypasses a given protection as a trusted user."""
        if self.role_level >= int(Role.ADMIN):
            return True
        return self.is_trusted and protection in (self.trust_bypass or [])


class PermissionDenied(Exception):
    def __init__(self, message: str = "⛔️ شما اجازه انجام این کار را ندارید."):
        super().__init__(message)
        self.message = message


# The ids Telegram uses when an administrator writes "anonymously".
ANONYMOUS_ADMIN_IDS = frozenset({1087968824, 136817688})
ANONYMOUS_ADMIN_USERNAMES = frozenset({"groupanonymousbot", "channelanonymousbot"})


def is_anonymous_sender(user: Any) -> bool:
    """True when the message was written by an anonymous group admin."""
    if user is None:
        return False
    if getattr(user, "is_anonymous_admin", False):
        return True
    if getattr(user, "id", None) in ANONYMOUS_ADMIN_IDS:
        return True
    username = (getattr(user, "username", "") or "").lower()
    return username in ANONYMOUS_ADMIN_USERNAMES


# --------------------------------------------------------------------------- #
# Telegram capability lookups
# --------------------------------------------------------------------------- #
async def _admin_from_list(bot: Bot, chat_id: int, user_id: int) -> ChatMember | None:
    """Fallback: find the user in ``getChatAdministrators``.

    ``getChatMember`` can fail transiently; the administrators list is a solid
    second source and is cached for the same short window.
    """
    cached = cache.admin_rights_cache.get((chat_id, user_id))
    if cached is not None:
        return cached
    try:
        admins = await bot.get_chat_administrators(chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_chat_administrators failed chat=%s: %s", chat_id, exc)
        return None
    for item in admins or []:
        user = getattr(item, "user", None)
        if user is not None and user.id == user_id:
            cache.admin_rights_cache.set((chat_id, user_id), item)
            return item
    return None


async def fetch_chat_member(bot: Bot, chat_id: int, user_id: int,
                            *, refresh: bool = False) -> ChatMember | None:
    key = (chat_id, user_id)
    if not refresh:
        cached = cache.member_status_cache.get(key)
        if cached is not None:
            return cached
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_chat_member failed chat=%s user=%s: %s", chat_id, user_id, exc)
        return None
    cache.member_status_cache.set(key, member)
    return member


def _apply_member(actor: Actor, member: ChatMember | None) -> Actor:
    if member is None:
        return actor
    actor.rights_known = True
    actor.telegram_status = member.status
    if isinstance(member, ChatMemberOwner):
        actor.telegram_status = "creator"
        actor.can_manage_chat = True
        actor.can_delete_messages = True
        actor.can_restrict_members = True
        actor.can_promote_members = True
        actor.can_change_info = True
        actor.can_invite_users = True
        actor.can_pin_messages = True
        actor.can_manage_video_chats = True
        actor.can_post_messages = True
        actor.can_edit_messages = True
        return actor
    if isinstance(member, ChatMemberAdministrator):
        actor.can_manage_chat = True
        actor.can_delete_messages = bool(member.can_delete_messages)
        actor.can_restrict_members = bool(member.can_restrict_members)
        actor.can_promote_members = bool(member.can_promote_members)
        actor.can_change_info = bool(member.can_change_info)
        actor.can_invite_users = bool(member.can_invite_users)
        actor.can_pin_messages = bool(member.can_pin_messages)
        actor.can_manage_video_chats = bool(member.can_manage_video_chats)
        actor.can_post_messages = bool(member.can_post_messages)
        actor.can_edit_messages = bool(member.can_edit_messages)
    return actor


async def build_actor(bot: Bot, chat_id: int, user_id: int, *, chat_type: str = "group",
                      session=None, from_user=None) -> Actor:
    """Build the :class:`Actor` context for a caller (cached where possible)."""
    actor = Actor(user_id=user_id, chat_id=chat_id, chat_type=chat_type)
    if chat_type == "private":
        actor.telegram_status = "member"
        if actor.is_bot_owner:
            actor.telegram_status = "creator"
        actor.bot_role = "founder" if actor.is_bot_owner else None
        return actor

    if is_anonymous_sender(from_user):
        # Telegram hides the identity, but only an administrator may post
        # anonymously - so the caller is treated as a full administrator.
        actor.anonymous_admin = True
        actor.is_anonymous_admin = True
        actor.telegram_status = "administrator"
        for field_name in ("can_manage_chat", "can_delete_messages",
                           "can_restrict_members", "can_promote_members",
                           "can_change_info", "can_invite_users", "can_pin_messages",
                           "can_manage_video_chats", "can_post_messages",
                           "can_edit_messages"):
            setattr(actor, field_name, True)
        actor.rights_known = True
        actor.bot_role = "founder"
        return actor

    member = await fetch_chat_member(bot, chat_id, user_id)
    if member is None:
        member = await _admin_from_list(bot, chat_id, user_id)
    _apply_member(actor, member)
    if from_user is not None:
        actor.is_anonymous_admin = bool(getattr(from_user, "is_anonymous_admin", False))
    if session is not None:
        from .chat_state import get_member_state_snapshot

        state = await get_member_state_snapshot(session, chat_id, user_id)
        actor.bot_role = state.get("bot_role")
        actor.is_trusted = bool(state.get("is_trusted"))
        actor.trust_bypass = list(state.get("trust_bypass") or [])
    if actor.is_bot_owner and not actor.is_telegram_admin:
        # Owner without Telegram admin rights keeps read access but no powers.
        actor.bot_role = "founder" if settings.owner_founder_in_groups else actor.bot_role
    return actor


async def bot_has_right(bot: Bot, chat_id: int, right: str) -> bool:
    """Does the **bot** hold this administrator right in the chat?

    Used to explain exactly which permission is missing instead of failing
    silently with a generic Telegram error.
    """
    member = await fetch_chat_member(bot, chat_id, bot.id)
    if member is None:
        return False
    if isinstance(member, ChatMemberOwner):
        return True
    if getattr(member, right, False):
        return True
    # A cached "no" may be stale (an admin may have just granted the right),
    # so re-ask Telegram once before reporting a missing permission.
    member = await fetch_chat_member(bot, chat_id, bot.id, refresh=True)
    if member is None:
        return False
    if isinstance(member, ChatMemberOwner):
        return True
    return bool(getattr(member, right, False))


async def bot_actor_with_rights(bot: Bot, chat_id: int) -> Actor:
    return await get_bot_actor(bot, chat_id)


async def get_bot_actor(bot: Bot, chat_id: int) -> Actor:
    """Actor describing the bot itself (used to verify it has the rights)."""
    member = await fetch_chat_member(bot, chat_id, bot.id)
    actor = Actor(user_id=bot.id, chat_id=chat_id)
    _apply_member(actor, member)
    return actor


# --------------------------------------------------------------------------- #
# Target protection
# --------------------------------------------------------------------------- #
class TargetProtection:
    """Reasons a target may not be moderated."""

    OK = ""
    BOT_ITSELF = "⛔️ امکان اعمال محدودیت روی خود ربات وجود ندارد."
    SELF_ACTION = "⛔️ نمی‌توانید روی حساب خودتان اخطار، سکوت یا بن اعمال/لغو کنید."
    CHAT_OWNER = "⛔️ مالک گروه قابل محدودسازی نیست."
    OTHER_ADMIN = "⛔️ کاربر هدف یکی از مدیران گروه است."
    MODERATION_ADMIN_ONLY = "⛔️ فقط مالک گروه می‌تواند روی ادمین‌های گروه اقدام کند."
    UNKNOWN_TARGET = "⚠️ وضعیت هدف مشخص نشد؛ برای بررسی جایگاه ادمین دوباره تلاش کنید."
    BOT_OWNER = "⛔️ مالک ربات قابل محدودسازی نیست."
    NOT_MEMBER = "❌ کاربر هدف در این گروه حضور ندارد."
    DELETED = "❌ حساب کاربر هدف حذف شده است."


async def check_target(bot: Bot, chat_id: int, target_id: int, actor: Actor,
                       *, allow_admin: bool = False) -> str:
    """Return an empty string when the target may be moderated by ``actor``."""
    if target_id == bot.id:
        return TargetProtection.BOT_ITSELF
    if settings.protect_bot_owner and settings.owner_id and target_id == settings.owner_id:
        # Only for private/self-hosted bots: a public bot must be able to
        # moderate the owner inside other people's groups.
        return TargetProtection.BOT_OWNER
    member = await fetch_chat_member(bot, chat_id, target_id)
    if member is None:
        return ""  # unknown target: let Telegram decide and report errors later
    if member.status in {"left", "kicked"}:
        return TargetProtection.NOT_MEMBER
    if member.status == "creator":
        return TargetProtection.CHAT_OWNER
    if member.status == "administrator" and not allow_admin:
        # An admin holding "add new admins" may manage other admins the same
        # way Telegram itself allows; the founder/creator always may.
        if actor.role_level >= int(Role.FOUNDER) or actor.anonymous_admin:
            return ""
        if actor.can_promote_members:
            return ""
        return TargetProtection.OTHER_ADMIN
    return ""


async def check_moderation_target(bot: Bot, session, chat_id: int, target_id: int,
                                 actor: Actor, *, allow_absent: bool = False) -> str:
    """Apply the moderation-specific hierarchy without changing other tools.

    The Telegram chat creator is the only person who may moderate a Telegram
    administrator.  An actor may never moderate themselves.  ``allow_absent``
    is used only by unban, which must also work for users who have already left.
    """
    if target_id == actor.user_id:
        return TargetProtection.SELF_ACTION

    base_reason = await check_target(bot, chat_id, target_id, actor)
    if base_reason in {TargetProtection.BOT_ITSELF, TargetProtection.BOT_OWNER,
                       TargetProtection.CHAT_OWNER}:
        return base_reason

    target_actor = await build_actor(bot, chat_id, target_id, session=session)
    if not target_actor.rights_known and not actor.is_group_creator:
        return TargetProtection.UNKNOWN_TARGET
    if target_actor.is_group_creator:
        return TargetProtection.CHAT_OWNER
    if target_actor.is_telegram_admin and not actor.is_group_creator:
        return TargetProtection.MODERATION_ADMIN_ONLY

    if allow_absent and base_reason == TargetProtection.NOT_MEMBER:
        return ""
    return base_reason


def require(condition: bool, message: str = "⛔️ شما اجازه انجام این کار را ندارید.") -> None:
    if not condition:
        raise PermissionDenied(message)


def normalize_permission_key(text: str) -> str:
    return normalize_text(text, mode="command")
