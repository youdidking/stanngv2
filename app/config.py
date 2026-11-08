"""Central configuration for Armando Group Manager.

All secrets are loaded from environment variables (or a local ``.env`` file).
Nothing sensitive is ever hardcoded in the source tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def _load_dotenv(path: Path | None = None) -> None:
    """Minimal ``.env`` parser (no external dependency required)."""
    env_path = path or BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Never override a real environment variable (Railway / Docker style).
        os.environ.setdefault(key, value)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "فعال"}


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    """Runtime configuration container."""

    # ---------------------------------------------------------------- required
    bot_token: str = ""
    owner_id: int = 0

    # ---------------------------------------------------------------- database
    database_url: str = field(default_factory=lambda: f"sqlite+aiosqlite:///{DATA_DIR / 'armando.sqlite3'}")
    database_echo: bool = False
    db_pool_size: int = 10

    # ------------------------------------------------------------------ runtime
    environment: str = "production"  # production | development
    log_level: str = "INFO"
    timezone: str = "Asia/Tehran"
    drop_pending_updates: bool = True

    # ----------------------------------------------------------------- webhook
    use_webhook: bool = False
    webhook_url: str = ""
    webhook_path: str = "/webhook"
    webhook_secret: str = "armando-secret-change-me"
    webapp_host: str = "0.0.0.0"
    webapp_port: int = 8080

    # -------------------------------------------------------------- log channel
    log_chat_id: int | None = None
    error_log_chat_id: int | None = None

    # ------------------------------------------------------------------- redis
    redis_url: str = ""

    # ---------------------------------------------------------------------- ai
    ai_provider: str = ""  # openai | openrouter | sightengine | ""
    ai_api_key: str = ""
    ai_model: str = "gpt-4o-mini"
    ai_enabled_default: bool = False
    ai_timeout: int = 12
    ai_score_threshold: float = 0.75

    # --------------------------------------------------- OpenAI-compatible AI chat
    ai_chat_api_key: str = ""
    ai_chat_base_url: str = ""
    ai_chat_default_model: str = ""  # optional private-chat fallback; groups choose their own
    # Backward-compatible aliases from the earlier TokenRouter-only setup.
    tokenrouter_api_key: str = ""
    tokenrouter_base_url: str = ""
    tokenrouter_default_model: str = ""

    # ---------------------------------------------------------- MTProto scan
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    # Optional user-authorized MTProto session, stored only as a Railway Secret.
    telegram_session_string: str = ""
    deleted_account_scan_interval: int = 86400
    deleted_account_max_pages: int = 500

    # --------------------------------------------------------- Instagram media
    # Optional Base64 Netscape cookie file; only used in the owner's private chat.
    instagram_cookies_b64: str = ""

    # ------------------------------------------------------------- market / apis
    market_cache_ttl: int = 90
    market_provider_order: str = "tgju,coingecko,erapi,nobitex,wallex"
    nobitex_api_url: str = "https://api.nobitex.ir"
    wallex_api_url: str = "https://api.wallex.ir"
    time_api_url: str = "https://timeapi.io/api/Time/current/zone?timeZone=Asia/Tehran"
    enable_remote_time_sync: bool = True

    # ------------------------------------------------------------------- branding
    creator_label: str = "Stan"       # shown as an inline button in /start
    creator_username: str = "RVIVL"   # shown as an inline button in the UI
    creator_url: str = "https://t.me/rvivl"

    # --------------------------------------------------------------- moderation
    global_ban_enabled: bool = False  # owner-only, OFF by default (see spec §35)
    # Opt-in gate: only groups the bot owner approved may use the bot.
    # OFF by default because Armando is a *public* multi-tenant bot.
    require_group_approval: bool = False
    # Public bots: the bot owner is an ordinary user in other people's groups.
    protect_bot_owner: bool = False
    owner_founder_in_groups: bool = False
    default_warn_limit: int = 4
    default_warn_action: str = "mute"
    antiflood_default_count: int = 5
    antiflood_default_window: int = 3

    # -------------------------------------------------------------- rate limits
    command_rate_limit: int = 12  # commands per user per window
    command_rate_window: int = 20
    callback_rate_limit: int = 30
    callback_rate_window: int = 10
    report_rate_limit: int = 3
    report_rate_window: int = 300
    captcha_attempt_cooldown: int = 2

    # ------------------------------------------------------------------ cleanup
    cache_cleanup_interval: int = 120
    scheduler_tick: int = 20
    purge_batch_size: int = 100

    # ------------------------------------------------------------------ methods
    @classmethod
    def from_env(cls, dotenv: bool = True) -> "Settings":
        if dotenv:
            _load_dotenv()
        values: dict[str, Any] = {}
        for f in fields(cls):
            values[f.name] = os.environ.get(f.name.upper(), None)

        settings = cls(
            bot_token=(values.get("bot_token") or "").strip(),
            owner_id=_as_int(values.get("owner_id")) or 0,
            database_url=(values.get("database_url") or "").strip()
            or f"sqlite+aiosqlite:///{DATA_DIR / 'armando.sqlite3'}",
            database_echo=_as_bool(values.get("database_echo")),
            db_pool_size=_as_int(values.get("db_pool_size")) or 10,
            environment=(values.get("environment") or "production").strip(),
            log_level=(values.get("log_level") or "INFO").strip().upper(),
            timezone=(values.get("timezone") or "Asia/Tehran").strip(),
            drop_pending_updates=_as_bool(values.get("drop_pending_updates"), True),
            use_webhook=_as_bool(values.get("use_webhook")),
            webhook_url=(values.get("webhook_url") or "").strip(),
            webhook_path=(values.get("webhook_path") or "/webhook").strip(),
            webhook_secret=(values.get("webhook_secret") or "armando-secret-change-me").strip(),
            webapp_host=(values.get("webapp_host") or "0.0.0.0").strip(),
            webapp_port=_as_int(values.get("webapp_port")) or 8080,
            log_chat_id=_as_int(values.get("log_chat_id")),
            error_log_chat_id=_as_int(values.get("error_log_chat_id")),
            redis_url=(values.get("redis_url") or "").strip(),
            ai_provider=(values.get("ai_provider") or "").strip(),
            ai_api_key=(values.get("ai_api_key") or "").strip(),
            ai_model=(values.get("ai_model") or "gpt-4o-mini").strip(),
            ai_enabled_default=_as_bool(values.get("ai_enabled_default")),
            ai_timeout=_as_int(values.get("ai_timeout")) or 12,
            ai_score_threshold=float(values.get("ai_score_threshold") or 0.75),
            ai_chat_api_key=(values.get("ai_chat_api_key")
                             or values.get("tokenrouter_api_key") or "").strip(),
            ai_chat_base_url=(values.get("ai_chat_base_url")
                              or values.get("tokenrouter_base_url")
                              or ("https://api.tokenrouter.com/v1"
                                  if values.get("tokenrouter_api_key") else "")).strip().rstrip("/"),
            ai_chat_default_model=(values.get("ai_chat_default_model")
                                   or values.get("tokenrouter_default_model") or "").strip(),
            tokenrouter_api_key=(values.get("tokenrouter_api_key") or "").strip(),
            tokenrouter_base_url=(values.get("tokenrouter_base_url") or "").strip().rstrip("/"),
            tokenrouter_default_model=(values.get("tokenrouter_default_model") or "").strip(),
            telegram_api_id=_as_int(values.get("telegram_api_id")),
            telegram_api_hash=(values.get("telegram_api_hash") or "").strip(),
            telegram_session_string=(values.get("telegram_session_string") or "").strip(),
            deleted_account_scan_interval=max(
                3600, _as_int(values.get("deleted_account_scan_interval")) or 86400),
            deleted_account_max_pages=max(
                1, min(2000, _as_int(values.get("deleted_account_max_pages")) or 500)),
            instagram_cookies_b64=(values.get("instagram_cookies_b64") or "").strip(),
            market_cache_ttl=_as_int(values.get("market_cache_ttl")) or 90,
            market_provider_order=(values.get("market_provider_order")
                                   or "tgju,coingecko,erapi,nobitex,wallex").strip(),
            nobitex_api_url=(values.get("nobitex_api_url") or "https://api.nobitex.ir").strip(),
            wallex_api_url=(values.get("wallex_api_url") or "https://api.wallex.ir").strip(),
            time_api_url=(values.get("time_api_url")
                          or "https://timeapi.io/api/Time/current/zone?timeZone=Asia/Tehran").strip(),
            enable_remote_time_sync=_as_bool(values.get("enable_remote_time_sync"), True),
            creator_label=(values.get("creator_label") or "Stan").strip(),
            creator_username=(values.get("creator_username") or "RVIVL").strip(),
            creator_url=(values.get("creator_url") or "https://t.me/rvivl").strip(),
            global_ban_enabled=_as_bool(values.get("global_ban_enabled")),
            require_group_approval=_as_bool(values.get("require_group_approval")),
            protect_bot_owner=_as_bool(values.get("protect_bot_owner")),
            owner_founder_in_groups=_as_bool(values.get("owner_founder_in_groups")),
            default_warn_limit=_as_int(values.get("default_warn_limit")) or 4,
            default_warn_action=(values.get("default_warn_action") or "mute").strip(),
            antiflood_default_count=_as_int(values.get("antiflood_default_count")) or 5,
            antiflood_default_window=_as_int(values.get("antiflood_default_window")) or 3,
            command_rate_limit=_as_int(values.get("command_rate_limit")) or 12,
            command_rate_window=_as_int(values.get("command_rate_window")) or 20,
            callback_rate_limit=_as_int(values.get("callback_rate_limit")) or 30,
            callback_rate_window=_as_int(values.get("callback_rate_window")) or 10,
            report_rate_limit=_as_int(values.get("report_rate_limit")) or 3,
            report_rate_window=_as_int(values.get("report_rate_window")) or 300,
            captcha_attempt_cooldown=_as_int(values.get("captcha_attempt_cooldown")) or 2,
            cache_cleanup_interval=_as_int(values.get("cache_cleanup_interval")) or 120,
            scheduler_tick=_as_int(values.get("scheduler_tick")) or 20,
            purge_batch_size=_as_int(values.get("purge_batch_size")) or 100,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.bot_token:
            raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    def safe_dict(self) -> dict[str, Any]:
        """Dump for logs/backups - secrets are masked or removed."""
        secret_keys = {"bot_token", "ai_api_key", "ai_chat_api_key", "tokenrouter_api_key",
                       "telegram_api_hash", "telegram_session_string",
                       "instagram_cookies_b64", "webhook_secret",
                       "database_url", "redis_url"}
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        for key in secret_keys:
            if data.get(key):
                data[key] = "***masked***"
        return data


settings = Settings.from_env(dotenv=False)  # populated later by ``load_settings``


def load_settings(dotenv: bool = True) -> Settings:
    """Load (or reload) the global settings object."""
    global settings
    settings = Settings.from_env(dotenv=dotenv)
    return settings
