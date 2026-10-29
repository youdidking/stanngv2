"""Persian-first text normalization layer.

This module is the single source of truth for every textual comparison in the
bot: command recognition, blacklist matching, filter matching and anti-evasion
detection.  It is intentionally dependency free so that it stays fast and can
be reused by any service without importing aiogram.

Three normalization strengths are provided:

* ``display``  - safe cleanup for text that is shown back to the user.
* ``command``  - cleanup used before matching Persian commands.
* ``aggressive`` - anti-evasion cleanup used by moderation filters.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# --------------------------------------------------------------------------- #
# Character maps
# --------------------------------------------------------------------------- #
ARABIC_YEH = ("\u064a", "\u06cc")  # ي  ې
PERSIAN_YEH = "\u06cc"
ARABIC_KEH = "\u0643"
PERSIAN_KEH = "\u06a9"

TATWEEL = "\u0640"
ZERO_WIDTH = ("\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\ufeff", "\u2060", "\u180e")

# Arabic diacritics (harakat) and Quranic marks.
DIACRITICS_RE = re.compile(
    "[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e8\u06ea-\u06ed]"
)

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
ASCII_DIGITS = "0123456789"

# Homoglyph / look-alike characters frequently abused to evade filters.
HOMOGLYPHS: dict[str, str] = {
    # Arabic -> Persian
    "\u064a": "\u06cc",  # yeh
    "\u0643": "\u06a9",  # keheh
    "\u06c1": "\u0647",  # heh goal
    "\u0629": "\u0647",  # teh marbuta -> heh
    "\u0670": "\u0627",  # dagger alef
    "\u0622": "\u0627",  # alef madda
    "\u0623": "\u0627",  # alef hamza above
    "\u0625": "\u0627",  # alef hamza below
    "\u0627\u064b": "\u0627",  # alef fathatan
    "\u0649": "\u06cc",  # alef maksura
    "\u0648\u064b": "\u0648",
    # Cyrillic look-alikes (used against Latin blacklist words)
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p",
    "\u0441": "c", "\u0443": "y", "\u0445": "x", "\u0456": "i",
    "\u0455": "s", "\u04bb": "h", "\u0501": "d",
    # Greek look-alikes
    "\u03b1": "a", "\u03bf": "o", "\u03bd": "v", "\u03c4": "t",
    # Full width / mathematical
    "\uff41": "a", "\uff4b": "k", "\uff53": "s",
}

# Leet / digit substitutions - only applied in aggressive (anti-evasion) mode.
LEET_MAP: dict[str, str] = {
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b",
    "9": "g", "@": "a", "$": "s", "!": "i", "|": "i", "\u0660": "o",
    "\u06f0": "o", "\u06f1": "i", "\u06f3": "e", "\u06f5": "s",
}

PUNCT_RE = re.compile(
    r"[,\.\-_/\\|!؟?\u061f\u060c\u061b:;\(\)\[\]{}<>\"'«»“”‘’…\*~`\^+=#@%&\u0640]"
)
EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff\u2b00-\u2bff\ufe0f\u2049\u203c]",
    flags=re.UNICODE,
)
SPACE_RE = re.compile(r"\s+")
REPEAT_RE = re.compile(r"(.)\1{2,}")

# Persian letter range (used to detect Persian/English script locks).
PERSIAN_LETTER_RE = re.compile(r"[\u0621-\u064a\u067e\u0686\u0698\u06a9\u06af\u06cc\u06c0]")
LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
# CJK ideographs + CJK punctuation (Chinese / Han based writing)
CJK_RE = re.compile(r"[\u2e80-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
# Devanagari + Devanagari extended (Hindi, Marathi, Nepali, Sanskrit)
DEVANAGARI_RE = re.compile(r"[\u0900-\u097f\ua8e0-\ua8ff]")

URL_RE = re.compile(
    r"(?:(?:https?://|ftp://|www\.)[^\s<>\[\]{}]+"
    r"|(?:t(?:elegram)?\.me|telegram\.dog|t\.me)/[A-Za-z0-9_\-+]+"
    r"|@[A-Za-z0-9_]{4,32}"
    r"|[A-Za-z0-9\-]+\.(?:com|net|org|ir|io|co|me|ru|xyz|top|click|link|tk|ml|ga|cf|app|site|online|biz))"
    r"[^\s<>\[\]{}]*",
    re.IGNORECASE,
)
TELEGRAM_LINK_RE = re.compile(
    r"(?:t\.me|telegram\.me|telegram\.dog)/(?:joinchat/|\+)?[A-Za-z0-9_\-]+", re.IGNORECASE
)
PHONE_RE = re.compile(r"(?:\+?\d[\d\-\s\(\)]{7,}\d)")
MENTION_RE = re.compile(r"@[A-Za-z0-9_]{3,32}")
HASHTAG_RE = re.compile(r"#[\w\u0600-\u06ff]+")
BOT_COMMAND_RE = re.compile(r"/[A-Za-z0-9_]{1,64}")


# --------------------------------------------------------------------------- #
# Low level helpers
# --------------------------------------------------------------------------- #
def remove_zero_width(text: str) -> str:
    for ch in ZERO_WIDTH:
        text = text.replace(ch, "")
    return text


def remove_diacritics(text: str) -> str:
    return DIACRITICS_RE.sub("", text)


def normalize_letters(text: str, aggressive: bool = False) -> str:
    """Fold Arabic variants into Persian and apply homoglyph folding."""
    out: list[str] = []
    for ch in text:
        if ch == ARABIC_KEH:
            out.append(PERSIAN_KEH)
            continue
        if ch in ARABIC_YEH:
            out.append(PERSIAN_YEH)
            continue
        if aggressive and ch in HOMOGLYPHS:
            out.append(HOMOGLYPHS[ch])
            continue
        out.append(ch)
    return "".join(out)


def normalize_digits(text: str, to: str = "ascii") -> str:
    """Convert Persian/Arabic digits to the requested digit set."""
    if to == "ascii":
        table = str.maketrans(PERSIAN_DIGITS + ARABIC_DIGITS, ASCII_DIGITS * 2)
    elif to == "fa":
        table = str.maketrans(ARABIC_DIGITS + ASCII_DIGITS, PERSIAN_DIGITS + PERSIAN_DIGITS)
    else:  # arabic-indic
        table = str.maketrans(PERSIAN_DIGITS + ASCII_DIGITS, ARABIC_DIGITS + ARABIC_DIGITS)
    return text.translate(table)


def to_persian_digits(text: str) -> str:
    return normalize_digits(text, to="fa")


def strip_emoji(text: str) -> str:
    return EMOJI_RE.sub("", text)


def collapse_repeats(text: str, max_repeat: int = 2) -> str:
    """کصصص -> کصص (keeps the text readable, kills spam padding)."""
    if max_repeat <= 0:
        return REPEAT_RE.sub(r"\1", text)
    return REPEAT_RE.sub(lambda m: m.group(1) * max_repeat, text)


def strip_separators(text: str) -> str:
    """Remove punctuation, separators and spaces: ``ک.ص`` -> ``کص``."""
    return SPACE_RE.sub("", PUNCT_RE.sub("", text))


def apply_leet(text: str) -> str:
    return "".join(LEET_MAP.get(ch, ch) for ch in text)


# --------------------------------------------------------------------------- #
# Public normalization entry points
# --------------------------------------------------------------------------- #
def normalize_text(text: str, mode: str = "command") -> str:
    """Normalize ``text`` with the requested strength.

    mode:
        ``display``    - unicode NFKC + zero-width/diacritic cleanup only
        ``command``    - display + letter folding, digit folding, whitespace
        ``aggressive`` - command + punctuation/homoglyph/leet folding
    """
    if not text:
        return ""
    mode = (mode or "command").lower()
    text = unicodedata.normalize("NFKC", text)
    text = remove_zero_width(text)
    text = remove_diacritics(text)
    if mode == "display":
        return SPACE_RE.sub(" ", text).strip()

    text = normalize_letters(text, aggressive=(mode == "aggressive"))
    text = text.replace(TATWEEL, "")
    text = normalize_digits(text, to="ascii")
    text = text.replace("\t", " ")
    text = SPACE_RE.sub(" ", text).strip()

    if mode == "aggressive":
        text = strip_emoji(text)
        text = unicodedata.normalize("NFKC", apply_leet(text.lower()))
        text = collapse_repeats(text, max_repeat=2)
        text = SPACE_RE.sub(" ", text).strip()
    else:
        text = text.lower()
    return text


def normalize_for_match(text: str) -> str:
    """Normalization used when comparing blacklist entries with real messages."""
    return normalize_text(text, mode="aggressive")


def normalize_command(text: str) -> str:
    return normalize_text(text, mode="command")


def collapse_letters(text: str) -> str:
    """Return a separator-free, punctuation-free form of ``text``."""
    return strip_separators(normalize_text(text, mode="aggressive"))


def tokenize(text: str) -> list[str]:
    return [t for t in SPACE_RE.split(normalize_command(text)) if t]


@lru_cache(maxsize=4096)
def word_variants(word: str) -> tuple[str, ...]:
    """Return every normalized form of ``word`` used for matching."""
    base = normalize_text(word, mode="aggressive")
    nospace = strip_separators(base)
    leet = apply_leet(normalize_digits(nospace, to="ascii"))
    collapsed = collapse_repeats(base, max_repeat=1)
    forms = {base, nospace, leet, collapsed, collapse_repeats(nospace, 1)}
    return tuple(sorted({f for f in forms if f}))


def contains_word(text: str, word: str) -> bool:
    """Word-boundary aware containment check with anti-evasion variants."""
    if not text or not word:
        return False
    haystack = normalize_text(text, mode="aggressive")
    haystack_nospace = strip_separators(haystack)
    for variant in word_variants(word):
        if not variant:
            continue
        if variant in haystack_nospace:
            return True
        # Word boundary aware check (Persian and Latin aware).
        pattern = r"(?<![\w\u0600-\u06ff])" + re.escape(variant) + r"(?![\w\u0600-\u06ff])"
        if re.search(pattern, haystack):
            return True
    return False


def contains_any(text: str, words: list[str] | tuple[str, ...] | set[str]) -> str | None:
    """Return the first matching entry of ``words`` inside ``text``."""
    haystack = normalize_text(text, mode="aggressive")
    haystack_nospace = strip_separators(haystack)
    for word in words:
        if not word or len(word) < 2:
            continue
        for variant in word_variants(word):
            if variant and variant in haystack_nospace:
                return word
    return None


# --------------------------------------------------------------------------- #
# Content detection helpers (used by the lock engine and anti-spam pipeline)
# --------------------------------------------------------------------------- #
def find_urls(text: str) -> list[str]:
    return URL_RE.findall(normalize_text(text, mode="command"))


def has_url(text: str) -> bool:
    return bool(URL_RE.search(normalize_text(text, mode="command")))


def has_telegram_link(text: str) -> bool:
    return bool(TELEGRAM_LINK_RE.search(normalize_text(text, mode="command")))


def has_mention(text: str) -> bool:
    return bool(MENTION_RE.search(text or ""))


def count_mentions(text: str) -> int:
    return len(MENTION_RE.findall(text or ""))


def has_hashtag(text: str) -> bool:
    return bool(HASHTAG_RE.search(text or ""))


def has_phone(text: str) -> bool:
    return bool(PHONE_RE.search(normalize_digits(text or "", to="ascii")))


def count_emojis(text: str) -> int:
    return len(EMOJI_RE.findall(text or ""))


def has_persian(text: str) -> bool:
    return bool(PERSIAN_LETTER_RE.search(text or ""))


def has_latin(text: str) -> bool:
    return bool(LATIN_LETTER_RE.search(text or ""))


def has_cyrillic(text: str) -> bool:
    return bool(CYRILLIC_RE.search(text or ""))


def has_chinese(text: str) -> bool:
    return bool(CJK_RE.search(text or ""))


def has_devanagari(text: str) -> bool:
    return bool(DEVANAGARI_RE.search(text or ""))


def has_command(text: str) -> bool:
    return bool(BOT_COMMAND_RE.search(text or ""))


def similarity(a: str, b: str) -> float:
    """Cheap similarity ratio in [0, 1] used by repeated-message detection."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    a_norm = normalize_text(a, mode="aggressive")
    b_norm = normalize_text(b, mode="aggressive")
    if a_norm == b_norm:
        return 1.0
    if len(a_norm) < 4 or len(b_norm) < 4:
        return 0.0
    longer, shorter = (a_norm, b_norm) if len(a_norm) >= len(b_norm) else (b_norm, a_norm)
    window = max(len(shorter) // 3, 2)
    best = 0
    for i in range(0, max(1, len(longer) - len(shorter) + 1), max(1, window // 2)):
        chunk = longer[i:i + len(shorter)]
        matches = sum(1 for x, y in zip(chunk, shorter) if x == y)
        best = max(best, matches)
    return round(best / len(shorter), 3)
