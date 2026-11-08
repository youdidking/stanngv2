"""Anti-profanity and anti-porn detectors (fully offline, deterministic)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..core.normalization import (
    collapse_repeats,
    normalize_text,
    strip_separators,
    to_persian_digits,
)

logger = logging.getLogger("armando.profanity")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

PROFANITY_CATEGORY_LABELS = {
    "fa": "فحاشی فارسی",
    "fa_phrases": "عبارت نامناسب",
    "az": "فحاشی ترکی",
    "en": "فحاشی انگلیسی",
    "en_phrases": "کلیدواژه انگلیسی",
}


@dataclass
class Match:
    word: str
    category: str
    label: str


@lru_cache(maxsize=1)
def _load_profanity() -> dict[str, list[str]]:
    path = DATA_DIR / "profanity.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, list) and not k.startswith("_")}
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to load profanity list: %s", exc)
        return {}


@lru_cache(maxsize=1)
def _load_porn() -> dict[str, list[str]]:
    path = DATA_DIR / "porn_keywords.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, list) and not k.startswith("_")}
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to load porn keywords: %s", exc)
        return {}


@lru_cache(maxsize=4096)
def _variants(word: str) -> tuple[str, ...]:
    """Normalized forms of a blacklisted word, including anti-evasion forms."""
    base = normalize_text(word, mode="aggressive")
    forms = {
        base,
        strip_separators(base),
        collapse_repeats(base, max_repeat=1),
        collapse_repeats(strip_separators(base), max_repeat=1),
    }
    forms.add(strip_separators(normalize_text(word, mode="command")))
    return tuple(sorted({f for f in forms if len(f) >= 2}))


def detect_profanity(text: str, *, categories: tuple[str, ...] | None = None) -> list[Match]:
    """Return every profanity hit found in ``text``."""
    if not text or len(text.strip()) < 2:
        return []
    data = _load_profanity()
    candidates = categories or tuple(data.keys())
    haystack = normalize_text(text, mode="aggressive")
    haystack_tight = strip_separators(haystack)
    haystack_collapsed = collapse_repeats(haystack_tight, max_repeat=1)

    matches: list[Match] = []
    for category in candidates:
        words = data.get(category) or []
        for word in words:
            for variant in _variants(word):
                if len(variant) < 2:
                    continue
                if variant in haystack_tight or variant in haystack_collapsed:
                    matches.append(Match(word=word, category=category,
                                         label=PROFANITY_CATEGORY_LABELS.get(category, category)))
                    break
    # De-duplicate by matched word
    seen: set[str] = set()
    unique: list[Match] = []
    for match in matches:
        key = normalize_text(match.word, mode="aggressive")
        if key in seen:
            continue
        seen.add(key)
        unique.append(match)
    return unique


def has_profanity(text: str, *, categories: tuple[str, ...] | None = None) -> bool:
    return bool(detect_profanity(text, categories=categories))


@dataclass
class PornMatch:
    keyword: str
    source: str  # keyword | pattern | filename | domain | media
    score: int


def detect_porn(text: str = "", *, file_name: str | None = None,
                strictness: int = 2) -> list[PornMatch]:
    """Heuristic adult-content detection.

    ``strictness``
        1 - only explicit keywords, 2 - keywords + patterns, 3 - everything.
    """
    data = _load_porn()
    matches: list[PornMatch] = []
    normalized = normalize_text(text or "", mode="aggressive")
    tight = strip_separators(normalized)

    if strictness >= 1:
        for lang in ("fa", "en"):
            for keyword in data.get(lang, []):
                for variant in _variants(keyword):
                    if len(variant) < 3:
                        continue
                    if variant in tight:
                        matches.append(PornMatch(keyword=keyword, source="keyword", score=2))
                        break
    if strictness >= 2:
        for pattern in data.get("patterns", []):
            try:
                if re.search(pattern, text or "", flags=re.IGNORECASE):
                    matches.append(PornMatch(keyword=pattern, source="pattern", score=2))
            except re.error:
                continue
        for domain in data.get("suspicious_domains", []):
            domain = domain.strip()
            if domain and domain.lower() in (text or "").lower():
                matches.append(PornMatch(keyword=domain, source="domain", score=3))
    if strictness >= 3 and file_name:
        lowered = file_name.lower()
        for pattern in data.get("file_name_patterns", []):
            try:
                if re.search(pattern, lowered, flags=re.IGNORECASE):
                    matches.append(PornMatch(keyword=file_name, source="filename", score=1))
            except re.error:
                continue
    return matches


def porn_score(matches: list[PornMatch]) -> int:
    return sum(m.score for m in matches)


def profanity_summary(matches: list[Match]) -> str:
    if not matches:
        return ""
    words = ", ".join(dict.fromkeys(m.word for m in matches[:5]))
    return to_persian_digits(f"{len(matches)}") + f" مورد: {words}"


def reload_lists() -> None:
    _load_profanity.cache_clear()
    _load_porn.cache_clear()
    _variants.cache_clear()


def stats() -> dict[str, int]:
    prof = _load_profanity()
    porn = _load_porn()
    return {
        "profanity_categories": len(prof),
        "profanity_words": sum(len(v) for v in prof.values()),
        "porn_keywords": sum(len(v) for v in porn.values() if isinstance(v, list)),
    }
