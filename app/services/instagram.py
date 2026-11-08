"""Download Instagram media with public extractors and authorized group/owner retries."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.types import (FSInputFile, InputMediaDocument, InputMediaPhoto,
                           InputMediaVideo, Message)

logger = logging.getLogger("armando.instagram")

_PUBLIC_POST_RE = re.compile(
    r"^https?://(?:www\.|m\.)?instagram\.com/(?:p|reel|reels|tv)/"
    r"[A-Za-z0-9_-]+/?(?:\?[^\s]*)?$",
    re.IGNORECASE,
)
_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".mp4", ".m4v", ".mov", ".webm", ".mkv",
}
# Instagram carousels may contain more than one Telegram media-group batch.
_MAX_FILES = 20
_ALBUM_BATCH_SIZE = 10
_MAX_TOTAL_BYTES = 48 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 55
_DOWNLOAD_COOLDOWN = 10.0
_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)
_last_download: dict[int, float] = {}
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_COOKIE_VALUE_RE = re.compile(
    r"(?i)\b(sessionid|csrftoken|ds_user_id|mid|ig_did)\s*[:=]\s*([^;\s,]+)"
)


@dataclass(frozen=True)
class _ExtractorAttempt:
    extractor: str
    return_code: int | None
    category: str
    detail: str
    files: tuple[Path, ...]
    operator_session: bool = False


def extract_public_instagram_url(text: str) -> str | None:
    """Accept only a standalone direct public post/reel URL."""
    candidate = (text or "").strip().strip("<> \t\r\n.,،؛!?؟")
    if _PUBLIC_POST_RE.fullmatch(candidate):
        return candidate
    return None


def _media_file_list(root: Path) -> list[Path]:
    files = [path for path in root.rglob("*")
             if path.is_file() and path.suffix.lower() in _MEDIA_EXTENSIONS]

    def natural_key(path: Path) -> tuple[tuple[int, int | str], ...]:
        parts = re.split(r"(\d+)", path.relative_to(root).as_posix())
        return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                     for part in parts)

    files.sort(key=natural_key)
    if len(files) > _MAX_FILES:
        logger.warning("اینستاگرام %s فایل رسانه برگرداند؛ فقط %s فایل نخست پردازش می‌شود.",
                       len(files), _MAX_FILES)
    return files[:_MAX_FILES]


def _input_media(path: Path, caption: str = ""):
    media = FSInputFile(path)
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png"}:
        return InputMediaPhoto(media=media, caption=caption or None, parse_mode=None)
    if ext == ".mp4":
        return InputMediaVideo(media=media, caption=caption or None, parse_mode=None)
    return InputMediaDocument(media=media, caption=caption or None, parse_mode=None)


async def _send_single_media(bot: Bot, message: Message, path: Path, caption: str,
                             *, force_document: bool = False) -> None:
    common = {
        "chat_id": message.chat.id,
        "caption": caption or None,
        "reply_to_message_id": message.message_id,
        "parse_mode": None,
    }
    media = FSInputFile(path)
    ext = path.suffix.lower()
    if not force_document and ext in {".jpg", ".jpeg", ".png"}:
        await bot.send_photo(photo=media, **common)
    elif not force_document and ext == ".mp4":
        await bot.send_video(video=media, **common)
    else:
        await bot.send_document(document=media, **common)


async def _send_media_batches(bot: Bot, message: Message, files: list[Path],
                              caption: str) -> None:
    """Send albums in Telegram's max-10 batches without silently dropping items."""
    if not files:
        return
    media = [_input_media(path, caption if index == 0 else "")
             for index, path in enumerate(files)]
    force_document = len(media) > 1 and any(
        isinstance(item, InputMediaDocument) for item in media)
    if force_document:
        media = [InputMediaDocument(
            media=FSInputFile(path), caption=caption if index == 0 else None,
            parse_mode=None)
            for index, path in enumerate(files)]
    if len(media) == 1:
        await _send_single_media(bot, message, files[0], caption)
        return

    for start in range(0, len(media), _ALBUM_BATCH_SIZE):
        batch = media[start:start + _ALBUM_BATCH_SIZE]
        if len(batch) == 1:
            await _send_single_media(
                bot, message, files[start], caption if start == 0 else "",
                force_document=force_document)
        else:
            await bot.send_media_group(
                chat_id=message.chat.id,
                media=batch,
                reply_to_message_id=message.message_id,
            )


def _extractor_order(url: str) -> tuple[str, str, str]:
    """Prefer the video-focused extractor for Reels; keep parth-dl as a public fallback."""
    path = urlsplit(url).path.lower()
    if path.startswith(("/reel/", "/reels/", "/tv/")):
        return "yt-dlp", "gallery-dl", "parth-dl"
    return "gallery-dl", "yt-dlp", "parth-dl"


def _classify_failure(detail: str, *, timed_out: bool = False) -> str:
    if timed_out:
        return "timeout"
    value = (detail or "").lower()
    if any(token in value for token in ("429", "too many requests", "rate limit", "rate-limit")):
        return "rate-limited"
    if any(token in value for token in (
        "isn't available to everyone", "is not available to everyone",
        "can't be seen by certain audiences", "cannot be seen by certain audiences",
        "audience restriction", "audience-restricted",
    )):
        return "audience-restricted"
    if any(token in value for token in (
        "private", "login", "logged-in", "logged in", "authentication", "unauthorized",
        "401", "403", "checkpoint", "cookie", "empty media response",
    )):
        return "login-required"
    if any(token in value for token in ("not found", "404", "no results", "unavailable")):
        return "unavailable"
    return "extractor-error"


def _safe_diagnostic(raw: bytes | str | None, *, fallback: str = "") -> str:
    """Keep useful extractor detail while removing URLs and session-like values."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw or ""
    text = _ANSI_RE.sub("", text)
    text = _COOKIE_VALUE_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _URL_RE.sub("[پیوند]", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    detail = " ".join(lines[-3:]) if lines else fallback
    return detail[:500]


def _cookie_file_bytes() -> bytes | None:
    """Decode the operator's optional Netscape cookie file without logging it."""
    from .. import config

    encoded = config.settings.instagram_cookies_b64
    if not encoded:
        return None
    try:
        compact = "".join(encoded.split())
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("secret کوکی اینستاگرام Base64 معتبر نیست؛ حالت بی‌کوکی ادامه می‌یابد.")
        return None
    if not data.strip():
        return None
    first_lines = data.splitlines()[:3]
    if not any(line.strip() in {b"# Netscape HTTP Cookie File", b"# HTTP Cookie File"}
               for line in first_lines):
        logger.warning("فایل کوکی اینستاگرام باید با قالب Netscape باشد؛ حالت بی‌کوکی ادامه می‌یابد.")
        return None
    return data


def _is_owner_private_chat(message: Message) -> bool:
    """Allow the operator's session in the operator's own private chat."""
    from .. import config

    actor = message.from_user
    owner_id = config.settings.owner_id
    return bool(
        owner_id
        and actor is not None
        and actor.id == owner_id
        and message.chat.type == "private"
    )


def _cookie_retry_allowed(message: Message) -> bool:
    """Allow private owner retries and human requests in chats accepted by middleware."""
    from .. import config

    if not config.settings.owner_id:
        return False
    actor = message.from_user
    if actor is None or getattr(actor, "is_bot", False):
        return False
    if message.chat.type in {"group", "supergroup"}:
        # GroupApprovalMiddleware blocks unapproved chats when that gate is enabled.
        return True
    return _is_owner_private_chat(message)


def _write_cookie_file(root: Path, data: bytes) -> Path:
    path = root / "instagram-cookies.txt"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "wb") as cookie_file:
        cookie_file.write(data)
    return path


def _download_env(root: Path) -> dict[str, str]:
    # Keep the subprocess isolated: no inherited API tokens, proxy credentials,
    # browser profiles, user configs, or persistent cache are passed through.
    return {
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root),
        "XDG_CACHE_HOME": str(root),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "PYTHONUTF8": "1",
    }


def _build_command(extractor: str, output_dir: Path, url: str,
                   cookies_path: Path | None = None) -> list[str]:
    if extractor == "gallery-dl":
        command = [
            sys.executable, "-m", "gallery_dl", "--quiet", "--config-ignore",
            "--destination", str(output_dir),
        ]
    elif extractor == "yt-dlp":
        command = [
            sys.executable, "-m", "yt_dlp", "--ignore-config", "--quiet",
            "--no-warnings", "--no-progress", "--no-playlist",
            "--merge-output-format", "mp4",
            "--output", str(output_dir / "%(id)s.%(ext)s"),
        ]
    elif extractor == "parth-dl":
        if cookies_path is not None:
            raise ValueError("parth-dl fallback is intentionally anonymous-only")
        # Use the package API under this same Python interpreter; the subprocess
        # still receives only the sanitized temporary environment.
        runner = (
            "import sys; from parth_dl import download; "
            "download(sys.argv[1], output_path=sys.argv[2], verbose=False)"
        )
        return [sys.executable, "-c", runner, url, str(output_dir)]
    else:
        raise ValueError(f"unsupported Instagram extractor: {extractor}")

    if cookies_path is not None:
        command.extend(["--cookies", str(cookies_path)])
    command.append(url)
    return command


async def _run_extractor(extractor: str, url: str, output_dir: Path,
                         env: dict[str, str],
                         cookies_path: Path | None = None) -> _ExtractorAttempt:
    output_dir.mkdir(parents=True, exist_ok=True)
    command = _build_command(extractor, output_dir, url, cookies_path)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        stdout, stderr = await process.communicate()
        detail = _safe_diagnostic(stderr or stdout, fallback="مهلت دریافت به پایان رسید")
        return _ExtractorAttempt(
            extractor, process.returncode, "timeout", detail, (), cookies_path is not None)

    files = tuple(_media_file_list(output_dir))
    detail = _safe_diagnostic(stderr or stdout)
    if process.returncode == 0 and files:
        return _ExtractorAttempt(
            extractor, process.returncode, "ok", detail, files, cookies_path is not None)
    if process.returncode == 0 and not detail:
        detail = "استخراج‌گر بدون فایل رسانه پایان یافت"
    category = _classify_failure(detail)
    return _ExtractorAttempt(
        extractor, process.returncode, category, detail, files, cookies_path is not None)


def _log_attempt(attempt: _ExtractorAttempt) -> None:
    session_mode = "نشست مالک" if attempt.operator_session else "بی‌کوکی"
    logger.warning(
        "استخراج‌گر اینستاگرام %s ناموفق بود؛ حالت=%s، کد=%s، علت=%s، جزئیات=%s",
        attempt.extractor,
        session_mode,
        attempt.return_code,
        attempt.category,
        attempt.detail or "بدون توضیح از استخراج‌گر",
    )


async def _download_instagram_media(
    url: str,
    root: Path,
    *,
    allow_operator_cookies: bool,
) -> tuple[list[Path], str, list[_ExtractorAttempt]]:
    """Try public extractors first; use the owner's session only for gated posts."""
    env = _download_env(root)
    order = _extractor_order(url)
    attempts: list[_ExtractorAttempt] = []

    for extractor in order:
        attempt = await _run_extractor(
            extractor, url, root / extractor / "anonymous", env)
        attempts.append(attempt)
        if attempt.category == "ok":
            return list(attempt.files), "ok", attempts
        _log_attempt(attempt)
        # Do not probe other anonymous endpoints after an explicit audience gate
        # or rate limit. An authorized owner session may be retried below, but a
        # genuinely missing post is never treated as something to work around.
        if attempt.category in {"rate-limited", "audience-restricted"}:
            break

    access_gated = any(attempt.category in {"login-required", "audience-restricted"}
                       for attempt in attempts)
    hard_blocked = any(attempt.category in {"rate-limited", "unavailable"}
                       for attempt in attempts)
    if access_gated and hard_blocked:
        logger.info("نشست مالک برای این پیوند استفاده نشد؛ نتیجه شامل محدودیت نرخ یا محتوای ناموجود است.")
    elif access_gated and not allow_operator_cookies:
        logger.info("نشست مالک برای این درخواست مجاز نبود؛ چت خصوصی مالک یا گروه مجاز لازم است.")
    elif access_gated:
        cookie_data = _cookie_file_bytes()
        if cookie_data is None:
            logger.warning(
                "نشست مالک بارگذاری نشد؛ متغیر INSTAGRAM_COOKIES_B64 تنظیم نشده یا فایل Netscape معتبر نیست.")
        else:
            logger.info("تلاش دوبارهٔ اینستاگرام با نشست مالک آغاز شد؛ مقادیر کوکی ثبت نمی‌شوند.")
            cookie_path = _write_cookie_file(root, cookie_data)
            # Both established extractors get a chance with the same authorized
            # session. parth-dl is deliberately anonymous-only.
            for extractor in order:
                if extractor == "parth-dl":
                    continue
                attempt = await _run_extractor(
                    extractor, url, root / extractor / "authorized-session",
                    env, cookie_path)
                attempts.append(attempt)
                if attempt.category == "ok":
                    logger.info("اینستاگرام با استخراج‌گر %s و نشست مالک دریافت شد.", extractor)
                    return list(attempt.files), "ok", attempts
                _log_attempt(attempt)
                if attempt.category == "rate-limited":
                    break

    if attempts:
        # Prefer a clear auth/rate-limit explanation over a generic extractor error.
        categories = {attempt.category for attempt in attempts}
        for category in (
            "rate-limited", "audience-restricted", "login-required", "unavailable", "timeout",
        ):
            if category in categories:
                return [], category, attempts
        return [], attempts[-1].category, attempts
    return [], "extractor-error", attempts


async def _send_notice(bot: Bot, message: Message, text: str) -> None:
    try:
        await bot.send_message(chat_id=message.chat.id, text=text,
                               reply_to_message_id=message.message_id,
                               parse_mode=None, disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001
        logger.info("ارسال پیام نتیجهٔ اینستاگرام ممکن نشد: %s", exc.__class__.__name__)


async def handle_instagram_message(message: Message, bot: Bot) -> bool:
    """Handle one recognized URL; return False for ordinary non-Instagram text."""
    url = extract_public_instagram_url(message.text or message.caption or "")
    if url is None:
        return False

    actor = message.from_user
    actor_id = actor.id if actor is not None else message.chat.id
    now = time.monotonic()
    previous = _last_download.get(actor_id)
    if previous is not None and now - previous < _DOWNLOAD_COOLDOWN:
        await _send_notice(
            bot, message,
            "⏳ برای هر کاربر، درخواست دریافت بعدی ۱۰ ثانیه پس از درخواست قبلی ممکن است.")
        return True
    _last_download[actor_id] = now
    for old_actor, last_time in list(_last_download.items()):
        if now - last_time > 600:
            _last_download.pop(old_actor, None)

    try:
        with tempfile.TemporaryDirectory(prefix="armando-instagram-") as tmp:
            root = Path(tmp)
            cookie_retry_allowed = _cookie_retry_allowed(message)
            async with _DOWNLOAD_SEMAPHORE:
                files, failure, attempts = await _download_instagram_media(
                    url, root, allow_operator_cookies=cookie_retry_allowed)
            if not files:
                if failure == "rate-limited":
                    notice = (
                        "⏳ اینستاگرام دسترسی سرور را موقتاً محدود کرده است. "
                        "کمی بعد دوباره امتحان کنید؛ کوکی هم محدودیت نرخ را تضمینی رفع نمی‌کند."
                    )
                elif failure == "audience-restricted":
                    owner_session_attempted = any(attempt.operator_session for attempt in attempts)
                    hard_blocked = any(
                        attempt.category in {"rate-limited", "unavailable"} for attempt in attempts)
                    if owner_session_attempted:
                        notice = (
                            "⚠️ نشست اینستاگرامِ مالک امتحان شد، اما اینستاگرام هنوز دسترسی این پست را رد می‌کند. "
                            "حسابِ کوکی باید خودش همان پست را ببیند؛ ممکن است نشست نامعتبر باشد یا سرور "
                            "Railway پذیرفته نشود. محدودیت واقعی مخاطب را نمی‌توان تضمینی رفع کرد."
                        )
                    elif hard_blocked:
                        notice = (
                            "⚠️ اینستاگرام دسترسی را محدود کرد و به‌دلیل پاسخِ نرخ‌محدود/ناموجود، تلاش با کوکی "
                            "عمداً انجام نشد. کمی بعد یا با پیوندی که حساب مجاز می‌بیند امتحان کنید."
                        )
                    elif cookie_retry_allowed:
                        notice = (
                            "⚠️ اینستاگرام دسترسی این پست را محدود کرده است؛ نشست مالک در این درخواست "
                            "بارگذاری نشد. مدیر ربات متغیر INSTAGRAM_COOKIES_B64 را در محیط Production "
                            "و استقرار آخر بررسی کند."
                        )
                    else:
                        notice = (
                            "⚠️ اینستاگرام دسترسی این پست را محدود کرده است و نشست مالک برای این گفت‌وگو "
                            "مجاز نبود. در گروه مجاز بات یا گفت‌وگوی خصوصی مالک امتحان کنید."
                        )
                elif failure == "login-required":
                    notice = (
                        "🔒 اینستاگرام برای این پیوند ورود یا نشست معتبر خواسته است. "
                        "دریافت بی‌کوکیِ لینک‌های عمومی تضمین‌شده نیست."
                    )
                elif failure == "unavailable":
                    notice = (
                        "❌ پیوند خصوصی، حذف‌شده یا در دسترس نیست؛ فقط رسانه‌ای که "
                        "برای این ربات قابل مشاهده باشد دریافت می‌شود."
                    )
                elif failure == "timeout":
                    notice = "⏱ دریافت رسانه بیش از حد طول کشید. لطفاً بعداً دوباره امتحان کنید."
                else:
                    notice = (
                        "❌ دریافت ناموفق بود. پیوند مستقیمِ پست یا ریل را بررسی کنید "
                        "و بعداً دوباره امتحان کنید."
                    )
                await _send_notice(bot, message, notice)
                return True

            total_size = sum(path.stat().st_size for path in files)
            if total_size > _MAX_TOTAL_BYTES:
                await _send_notice(
                    bot, message,
                    "❌ حجم رسانه از سقف ارسال ربات در تلگرام بیشتر است.")
                return True

            caption = "دانلود شده توسط آرماندو آلخاندرو پرز"
            await _send_media_batches(bot, message, files, caption)
            return True
    except FileNotFoundError:
        await _send_notice(
            bot, message,
            "❌ ابزار دریافت رسانه روی سرور نصب نیست؛ مدیر ربات باید وابستگی‌ها را به‌روز کند.")
        return True
    except Exception as exc:  # noqa: BLE001 - external site and Telegram failures are expected
        logger.warning("دریافت یا ارسال رسانهٔ اینستاگرام ناموفق بود: %s",
                       exc.__class__.__name__)
        await _send_notice(
            bot, message,
            "❌ دریافت یا ارسال رسانه ناموفق شد. مطمئن شوید پیوند عمومی و رسانه در دسترس است.")
        return True
