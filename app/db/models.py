"""SQLAlchemy ORM models for Armando Group Manager.

Portable across SQLite (default) and PostgreSQL: every column uses portable
types, JSON payloads are stored with ``sqlalchemy.JSON`` and booleans use
``server_default`` values that work on both engines.
"""

from __future__ import annotations

from datetime import datetime, date

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TRUE = "1"
FALSE = "0"


class Base(DeclarativeBase):
    """Declarative base with a shared naming convention for constraints."""

    naming_convention = {
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


# --------------------------------------------------------------------------- #
# Core chat / user entities
# --------------------------------------------------------------------------- #
class Chat(Base, TimestampMixin):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    type: Mapped[str] = mapped_column(String(32), default="group", nullable=False)
    owner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    bot_is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    # Only read when REQUIRE_GROUP_APPROVAL=true: an unapproved group is ignored.
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    language_code: Mapped[str | None] = mapped_column(String(12), nullable=True)

    global_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    global_ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    global_banned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    afk: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    afk_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    afk_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    favorite_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    internal_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_blocked_by_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)

    __table_args__ = (Index("ix_users_username", "username"),)


class ChatMemberState(Base, TimestampMixin):
    """Per-chat state of a user: role data, warnings, activity, custom tag."""

    __tablename__ = "chat_members"
    __table_args__ = (
        Index("ix_chat_members_chat_user", "chat_id", "user_id"),
        Index("ix_chat_members_chat_activity", "chat_id", "last_message_at"),
        UniqueConstraint("chat_id", "user_id", name="uq_chat_members_chat_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="member", nullable=False)

    warn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    media_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    command_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")

    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    is_trusted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    trust_bypass: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # ["locks","filters",...]
    bot_role: Mapped[str | None] = mapped_column(String(32), nullable=True)

    tag: Mapped[str | None] = mapped_column(String(64), nullable=True)  # editable member tag
    mention_opt_out: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=FALSE)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    captcha_passed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    restricted_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    muted_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ChatSettings(Base, TimestampMixin):
    __tablename__ = "chat_settings"

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    language: Mapped[str] = mapped_column(String(8), default="fa", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Tehran", nullable=False)
    anonymous_sender_exempt: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="TRUE")

    # ------------------------------------------------------------------ warnings
    warn_limit: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    warn_action: Mapped[str] = mapped_column(String(24), default="mute", nullable=False)
    warn_action_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warn_expire_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warn_notify_user: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)

    # ----------------------------------------------------------------- antispam
    antispam_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    antiflood_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    antiflood_count: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    antiflood_window: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    antiflood_action: Mapped[str] = mapped_column(String(24), default="mute", nullable=False)
    antiflood_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    antispam_same_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0 = off
    antispam_same_action: Mapped[str] = mapped_column(String(24), default="warn", nullable=False)
    mention_spam_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mention_spam_action: Mapped[str] = mapped_column(String(24), default="mute", nullable=False)
    forward_spam_action: Mapped[str] = mapped_column(String(24), default="none", nullable=False)

    # ------------------------------------------------------------------ antiraid
    antiraid_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    antiraid_threshold: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    antiraid_window: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    antiraid_action: Mapped[str] = mapped_column(String(24), default="kick", nullable=False)
    antiraid_lock_new: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    antiraid_force_captcha: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    raid_mode_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ------------------------------------------------------------------ captcha
    captcha_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    captcha_mode: Mapped[str] = mapped_column(String(16), default="button", nullable=False)
    captcha_timeout: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    captcha_max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    captcha_fail_action: Mapped[str] = mapped_column(String(16), default="kick", nullable=False)
    captcha_delete_after: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)

    # ------------------------------------------------------------------ welcome
    welcome_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    welcome_text: Mapped[str] = mapped_column(
        Text,
        default=("╭─── ༺ خوش‌آمدید ༻ ───╮\n"
                 "🌷 سلام {mention} عزیز!\n\n"
                 "🆔 شناسهٔ شما: <code>{user_id}</code>\n"
                 "🔗 نام کاربری: <code>{username}</code>\n"
                 "💬 گروه: <b>{chat_name}</b>\n"
                 "🗓 زمان ورود: {date}، ساعت {time}\n\n"
                 "📜 لطفاً پیش از گفتگو، قوانین گروه را مطالعه کنید.\n"
                 "╰────────────────────╯"),
        nullable=False,
    )
    welcome_media: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    welcome_buttons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    welcome_delete_after: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    goodbye_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    goodbye_text: Mapped[str] = mapped_column(Text, default="👋 {first_name} از گروه رفت.", nullable=False)
    goodbye_media: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    goodbye_buttons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    goodbye_delete_after: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Legacy per-chat override is retained for schema compatibility; the global
    # outgoing-message lifecycle now enforces a fixed thirty-second group TTL.
    report_delete_after: Mapped[int] = mapped_column(
        Integer, default=30, nullable=False, server_default="30")

    # -------------------------------------------------------- service messages
    clean_join: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    clean_leave: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    clean_pin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    clean_service: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    clean_channel_post: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    clean_voice_chat: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)

    # ------------------------------------------------------------------- rules
    rules_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rules_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)

    # ---------------------------------------------------------- content safety
    anti_profanity_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    anti_profanity_action: Mapped[str] = mapped_column(String(24), default="delete_warn", nullable=False)
    anti_porn_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    anti_porn_action: Mapped[str] = mapped_column(String(24), default="delete_ban", nullable=False)
    anti_porn_strictness: Mapped[int] = mapped_column(Integer, default=2, nullable=False)  # 1..3

    # ---------------------------------------------------------- leave guard
    # Ban a member the moment they leave the group (anti leave-and-return).
    ban_on_leave: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    # Ban when somebody joins and leaves again within this many seconds (0=off)
    quick_leave_ban: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    quick_leave_seconds: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    # Send private details about departures/leave-guard bans to owner and the
    # bot-root delegates scoped to this group.
    leave_notify_owner: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=FALSE)

    # ------------------------------------------------------ sticky ban (anti rejoin)
    # When a member is removed (ban / kick) the ban stays in place, so the user
    # can never come back through an invite link until an admin unbans them.
    sticky_ban: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)

    # ------------------------------------------------------ identity change notices
    announce_name_change: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    announce_username_change: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)

    # ------------------------------------------------------- join request handling
    # Auto-approve «درخواست عضویت» in private groups (needs can_invite_users).
    auto_approve_join_requests: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)

    # ------------------------------------------------------------------- misc
    night_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    night_start: Mapped[str] = mapped_column(String(8), default="01:00", nullable=False)
    night_end: Mapped[str] = mapped_column(String(8), default="06:00", nullable=False)
    night_action: Mapped[str] = mapped_column(String(24), default="mute", nullable=False)
    night_bypass_roles: Mapped[list | None] = mapped_column(JSON, nullable=True)

    report_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    report_notify_admins: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    afk_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    games_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    market_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)

    log_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    log_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # ai_enabled is for moderation analysis only. Chat conversation selection is
    # independent and stored per group in ai_chat_model. The agent memory is an
    # explicit per-group opt-in and retains message text for seven days.
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    ai_threshold: Mapped[float] = mapped_column(Float, default=0.75, nullable=False)
    ai_chat_model: Mapped[str] = mapped_column(
        String(200), default="", nullable=False, server_default=text("''"))
    ai_agent_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=FALSE)

    filter_default_action: Mapped[str] = mapped_column(String(24), default="delete", nullable=False)
    filter_default_mode: Mapped[str] = mapped_column(String(16), default="word", nullable=False)

    trust_bypass: Mapped[list | None] = mapped_column(JSON, nullable=True)
    disabled_commands: Mapped[list | None] = mapped_column(JSON, nullable=True)

    federation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fed_enforce: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)

    tag_allow_user_edit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)


# --------------------------------------------------------------------------- #
# Roles, trust, moderation
# --------------------------------------------------------------------------- #
class BotRole(Base):
    __tablename__ = "bot_roles"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_bot_roles_chat_user"),
        Index("ix_bot_roles_chat", "chat_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="moderator", nullable=False)
    assigned_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TrustedUser(Base):
    __tablename__ = "trusted_users"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_trusted_chat_user"),
        Index("ix_trusted_chat", "chat_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    bypass: Mapped[list | None] = mapped_column(JSON, nullable=True)
    added_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Warning(Base):
    __tablename__ = "warnings"
    __table_args__ = (
        Index("ix_warnings_chat_user", "chat_id", "user_id"),
        Index("ix_warnings_chat_time", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    moderator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    action_taken: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ModerationAction(Base):
    __tablename__ = "moderation_actions"
    __table_args__ = (
        Index("ix_mod_actions_chat_time", "chat_id", "created_at"),
        Index("ix_mod_actions_chat_user", "chat_id", "target_id"),
        Index("ix_mod_actions_active", "active", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # ban/mute/kick/warn/unban/unmute
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)  # seconds
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    source: Mapped[str] = mapped_column(String(32), default="command", nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    undone_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class BanRecord(Base):
    __tablename__ = "ban_records"
    __table_args__ = (
        Index("ix_ban_records_chat_user", "chat_id", "user_id"),
        Index("ix_ban_records_scope", "scope", "active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    banned_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    scope: Mapped[str] = mapped_column(String(16), default="chat", nullable=False)  # chat|fed|global
    federation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Lock(Base):
    __tablename__ = "locks"
    __table_args__ = (UniqueConstraint("chat_id", "key", name="uq_locks_chat_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(48), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    action: Mapped[str] = mapped_column(String(24), default="delete", nullable=False)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                                                 nullable=False)


class FilterRule(Base):
    """Blocklist entry (is_blocklist=True) or custom trigger with a response."""

    __tablename__ = "filters"
    __table_args__ = (
        UniqueConstraint("chat_id", "trigger", name="uq_filters_chat_trigger"),
        Index("ix_filters_chat", "chat_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    trigger: Mapped[str] = mapped_column(String(255), nullable=False)
    is_blocklist: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    match_mode: Mapped[str] = mapped_column(String(16), default="word", nullable=False)  # word|exact|contains|regex
    action: Mapped[str] = mapped_column(String(24), default="delete", nullable=False)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)

    # custom (non blocklist) responses
    response_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    response_type: Mapped[str] = mapped_column(String(24), default="text", nullable=False)
    response_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    response_caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_buttons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cooldown: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    replies: Mapped[list | None] = mapped_column(JSON, nullable=True)  # random response sets

    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        UniqueConstraint("chat_id", "name", name="uq_notes_chat_name"),
        Index("ix_notes_chat", "chat_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    content_type: Mapped[str] = mapped_column(String(24), default="text", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    buttons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    noformat: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                                                 nullable=False)


class PersonalCommand(Base):
    __tablename__ = "personal_commands"
    __table_args__ = (
        UniqueConstraint("chat_id", "trigger", name="uq_personal_chat_trigger"),
        Index("ix_personal_commands_chat", "chat_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    trigger: Mapped[str] = mapped_column(String(128), nullable=False)
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    content_type: Mapped[str] = mapped_column(String(24), default="text", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    buttons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cooldown: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class MagicTrigger(Base):
    """Sticker / GIF triggers."""

    __tablename__ = "magic_triggers"
    __table_args__ = (
        UniqueConstraint("chat_id", "trigger_type", "file_unique_id", name="uq_magic_chat_file"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(16), default="sticker", nullable=False)
    file_unique_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_type: Mapped[str] = mapped_column(String(24), default="text", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    buttons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ReactionRule(Base):
    __tablename__ = "reaction_rules"
    __table_args__ = (Index("ix_reaction_rules_chat", "chat_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    emoji: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(32), default="reply", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class CaptchaSession(Base):
    __tablename__ = "captcha_sessions"
    __table_args__ = (
        Index("ix_captcha_chat_user", "chat_id", "user_id"),
        Index("ix_captcha_token", "token"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="button", nullable=False)
    answer: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    solved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    failed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=FALSE)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (Index("ix_reports_chat_time", "chat_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reporter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="open", nullable=False)
    handled_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ScheduledMessage(Base):
    __tablename__ = "scheduled_messages"
    __table_args__ = (Index("ix_scheduled_next_run", "enabled", "next_run_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    media: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    buttons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    repeat_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = one time
    cron: Mapped[str | None] = mapped_column(String(64), nullable=True)  # "HH:MM" daily or "weekly:5:HH:MM"
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Tehran", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Federation(Base):
    __tablename__ = "federations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rules: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ban_mode: Mapped[str] = mapped_column(String(16), default="share", nullable=False)  # share|enforce
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class FederationAdmin(Base):
    __tablename__ = "federation_admins"
    __table_args__ = (UniqueConstraint("federation_id", "user_id", name="uq_fed_admin"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    federation_id: Mapped[int] = mapped_column(Integer, ForeignKey("federations.id", ondelete="CASCADE"),
                                               nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class FederationMember(Base):
    __tablename__ = "federation_members"
    __table_args__ = (UniqueConstraint("federation_id", "chat_id", name="uq_fed_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    federation_id: Mapped[int] = mapped_column(Integer, ForeignKey("federations.id", ondelete="CASCADE"),
                                               nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    joined_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (Index("ix_connections_user", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=TRUE)
    connected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class UserActivity(Base):
    """Daily activity aggregate used for statistics and inactivity detection."""

    __tablename__ = "user_activity"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", "day", name="uq_activity_chat_user_day"),
        Index("ix_activity_chat_day", "chat_id", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    media_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    command_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")


class GroupStatSnapshot(Base):
    __tablename__ = "group_stats"
    __table_args__ = (UniqueConstraint("chat_id", "day", name="uq_group_stats_chat_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    joins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    leaves: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warns: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bans: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_chat_time", "chat_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class GroupMessageMemory(Base):
    """Short-lived text memory for groups that explicitly enable the AI agent."""

    __tablename__ = "group_message_memory"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_group_memory_chat_message"),
        Index("ix_group_memory_chat_time", "chat_id", "created_at"),
        Index("ix_group_memory_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    speaker_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    username: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    message_type: Mapped[str] = mapped_column(String(24), default="text", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PendingPrivateReport(Base):
    """Undelivered owner/root ban reports, retried when the user starts the bot."""

    __tablename__ = "pending_private_reports"
    __table_args__ = (
        Index("ix_pending_private_reports_recipient", "recipient_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipient_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    report_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class BotSettings(Base, TimestampMixin):
    """Small set of persistent bot-wide switches (currently broadcast)."""

    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    broadcast_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=TRUE)
    # Used only for the completion report delivered in the bot's private chat.
    broadcast_report_delete_after: Mapped[int] = mapped_column(
        Integer, default=12, nullable=False, server_default="12")
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class BroadcastDraft(Base):
    """Owner-reviewed broadcast source and destination selection."""

    __tablename__ = "broadcast_drafts"
    __table_args__ = (Index("ix_broadcast_drafts_owner_stage", "owner_id", "stage"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_message_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    text_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_chat_ids: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), default="select", nullable=False)
    page: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 onupdate=datetime.utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class BotRootGrant(Base):
    """A bot-owner delegation scoped to one chat and one Telegram admin."""

    __tablename__ = "bot_root_grants"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_bot_root_chat_user"),
        Index("ix_bot_root_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"),
                                         nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"),
                                         nullable=False)
    granted_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Legacy grants retain the existing broadcast-only behavior. New grants
    # are chosen explicitly from the bot-owner's capability menu.
    permissions: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["broadcast"], nullable=False,
        server_default=text("'[\"broadcast\"]'"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class GlobalMute(Base):
    """Persistent global mute plus per-chat restrictions created by it.

    The saved chat ids and prior permissions let global-unmute restore only
    restrictions this feature changed, without lifting a local admin's mute.
    """

    __tablename__ = "global_mutes"
    __table_args__ = (Index("ix_global_mutes_active", "active"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    muted_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="بدون دلیل", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False,
                                         server_default=TRUE)
    applied_chat_ids: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    previous_permissions: Mapped[dict[str, dict] | None] = mapped_column(
        JSON, default=dict, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 nullable=False)


class RootGrantDraft(Base):
    """Short-lived private selection flow for assigning root access to a chat."""

    __tablename__ = "root_grant_drafts"
    __table_args__ = (Index("ix_root_grant_drafts_owner", "owner_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 onupdate=datetime.utcnow, nullable=False)


ALL_MODELS: tuple[type[Base], ...] = (
    Chat, ChatSettings, User, ChatMemberState, BotRole, TrustedUser, Warning,
    ModerationAction, Lock, FilterRule, Note, PersonalCommand, MagicTrigger,
    ReactionRule, CaptchaSession, Report, ScheduledMessage, Federation,
    FederationAdmin, FederationMember, Connection, UserActivity,
    GroupStatSnapshot, AuditLog, GroupMessageMemory, PendingPrivateReport,
    BanRecord, BotSettings, BroadcastDraft, BotRootGrant, GlobalMute,
    RootGrantDraft, SchemaVersion,
)
