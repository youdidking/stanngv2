"""Persian entertainment module (isolated from moderation)."""

from __future__ import annotations

import json
import logging
import random
from functools import lru_cache
from pathlib import Path

from aiogram import Bot

from ..core.errors import safe_call
from ..core.normalization import normalize_digits, to_persian_digits

logger = logging.getLogger("armando.entertainment")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

SLOT_SYMBOLS = ["🍒", "🍋", "🔔", "⭐️", "7️⃣", "💎"]
COIN_SIDES = ["🦅 شیر", "✍️ خط"]


@lru_cache(maxsize=1)
def _load_jokes() -> list[str]:
    try:
        data = json.loads((DATA_DIR / "jokes.json").read_text(encoding="utf-8"))
        return list(data.get("jokes") or [])
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to load jokes: %s", exc)
        return []


@lru_cache(maxsize=1)
def _load_hafez() -> list[dict]:
    try:
        data = json.loads((DATA_DIR / "hafez.json").read_text(encoding="utf-8"))
        return list(data.get("poems") or [])
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to load hafez: %s", exc)
        return []


def random_joke() -> str:
    jokes = _load_jokes()
    if not jokes:
        return "😅 در حال حاضر جوکی در دسترس نیست."
    return random.choice(jokes)


def hafez_fal() -> tuple[str, str]:
    poems = _load_hafez()
    if not poems:
        return ("🌸 فعلاً دیوان در دسترس نیست؛ کمی بعد دوباره امتحان کن.", "")
    poem = random.choice(poems)
    verses = "\n".join(poem.get("verses") or [])
    return verses, (poem.get("interpretation") or "")


def fal_text() -> str:
    verses, interpretation = hafez_fal()
    lines = ["🔮 <b>فال حافظ</b>", "", verses]
    if interpretation:
        lines += ["", f"📖 تعبیر: {interpretation}"]
    return "\n".join(lines)


def flip_coin() -> str:
    return f"🪙 {random.choice(COIN_SIDES)}"


def roll_dice_text(sides: int = 6) -> str:
    return to_persian_digits(f"🎲 عدد {random.randint(1, max(2, sides))} آمد.")


def random_number(low: int = 1, high: int = 100) -> str:
    if low > high:
        low, high = high, low
    return to_persian_digits(f"🔢 عدد تصادفی: {random.randint(low, high)}")


def slot_machine() -> tuple[str, bool]:
    reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
    win = len(set(reels)) == 1
    text = "🎰 [ {} | {} | {} ]".format(*reels)
    if win:
        text += "\n\n🎉 تبریک! هر سه یکی شد."
    else:
        text += "\n\n😢 این بار نشد؛ دوباره امتحان کن."
    return text, win


async def send_dice(bot: Bot, chat_id: int, emoji: str = "🎲",
                    reply_to_message_id: int | None = None):
    return await safe_call(lambda: bot.send_dice(chat_id=chat_id, emoji=emoji,
                                                 reply_to_message_id=reply_to_message_id),
                           context="send_dice")


def parse_range(text: str) -> tuple[int, int] | None:
    """Parse ``عدد تصادفی ۱ ۱۰۰`` into ``(1, 100)``."""
    raw = normalize_digits(text or "", to="ascii").strip()
    parts = [p for p in raw.replace("تا", " ").split() if p.strip().isdigit()]
    if len(parts) >= 2:
        return int(parts[0]), int(parts[1])
    if len(parts) == 1:
        return 1, int(parts[0])
    return None


# --------------------------------------------------------------------------- #
# Tiny number guessing game (in memory, TTL bound)
# --------------------------------------------------------------------------- #
class GuessGame:
    def __init__(self, ttl: int = 600) -> None:
        self._games: dict[tuple[int, int], tuple[int, int, float]] = {}
        self.ttl = ttl

    def start(self, chat_id: int, user_id: int, high: int = 100) -> int:
        import time

        number = random.randint(1, max(2, high))
        self._games[(chat_id, user_id)] = (number, 0, time.monotonic())
        return number

    def guess(self, chat_id: int, user_id: int, value: int) -> tuple[str, bool]:
        import time

        entry = self._games.get((chat_id, user_id))
        if entry is None:
            return "ℹ️ ابتدا با <code>بازی</code> یک بازی جدید شروع کن.", False
        number, attempts, started = entry
        if time.monotonic() - started > self.ttl:
            self._games.pop((chat_id, user_id), None)
            return "⌛️ زمان این بازی تمام شد. دوباره شروع کن.", False
        attempts += 1
        if value == number:
            self._games.pop((chat_id, user_id), None)
            return (f"🎉 آفرین! عدد {to_persian_digits(str(number))} بود.\n"
                    f"🎯 تعداد تلاش: {to_persian_digits(str(attempts))}"), True
        hint = "بزرگ‌تر" if value < number else "کوچک‌تر"
        self._games[(chat_id, user_id)] = (number, attempts, started)
        return f"🔎 نه! عدد مورد نظر {hint} است. (تلاش {to_persian_digits(str(attempts))})", False

    def cleanup(self) -> int:
        import time

        now = time.monotonic()
        stale = [k for k, v in self._games.items() if now - v[2] > self.ttl]
        for key in stale:
            self._games.pop(key, None)
        return len(stale)


guess_game = GuessGame()
