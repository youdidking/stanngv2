"""Optional AI moderation layer.

Design rules (see project spec §45):

* Completely optional - the bot works with zero paid APIs.
* Never required for essential security functions.
* Failures (network, quota, parsing) always degrade to ``None`` so that the
  deterministic local moderation pipeline keeps running.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..config import settings

logger = logging.getLogger("armando.ai")

SYSTEM_PROMPT = (
    "You are a content moderation classifier for Persian (Farsi) group chats. "
    "Classify the message and answer ONLY with compact JSON: "
    '{"toxicity": <0..1>, "harassment": <0..1>, "spam": <0..1>, "adult": <0..1>, '
    '"reason": "<short Persian reason>"}. '
    "Be conservative: only score high when the intent is clearly abusive, sexual, "
    "or promotional spam."
)


@dataclass
class AIVerdict:
    score: float = 0.0
    categories: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    provider: str = ""

    @property
    def is_violation(self) -> bool:
        return self.score >= settings.ai_score_threshold


class AIModerator:
    """Thin wrapper around the configured provider (if any)."""

    def __init__(self) -> None:
        self.enabled = bool(settings.ai_provider and settings.ai_api_key)

    async def analyze(self, text: str) -> AIVerdict | None:
        if not self.enabled or not text or len(text.strip()) < 8:
            return None
        provider = settings.ai_provider.lower()
        try:
            if provider in {"openai", "openrouter", "groq", "together"}:
                return await self._chat_completion(text, provider)
            if provider == "sightengine":
                return await self._sightengine(text)
            logger.debug("unknown ai provider: %s", provider)
            return None
        except Exception as exc:  # noqa: BLE001 - AI must never break moderation
            logger.warning("ai moderation failed: %s", exc)
            return None

    # ---------------------------------------------------------------- providers
    async def _chat_completion(self, text: str, provider: str) -> AIVerdict | None:
        from ..services.market import get_http_session  # reuse the shared session

        base_urls = {
            "openai": "https://api.openai.com/v1/chat/completions",
            "openrouter": "https://openrouter.ai/api/v1/chat/completions",
            "groq": "https://api.groq.com/openai/v1/chat/completions",
            "together": "https://api.together.xyz/v1/chat/completions",
        }
        url = base_urls.get(provider)
        if not url:
            return None
        session = await get_http_session()
        payload = {
            "model": settings.ai_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text[:2000]},
            ],
            "temperature": 0,
            "max_tokens": 200,
        }
        headers = {"Authorization": f"Bearer {settings.ai_api_key}",
                   "Content-Type": "application/json"}
        async with session.post(url, json=payload, headers=headers,
                                timeout=settings.ai_timeout) as response:
            if response.status != 200:
                logger.debug("ai provider returned %s", response.status)
                return None
            data = await response.json(content_type=None)
        content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        return _parse_verdict(content, provider=provider)

    async def _sightengine(self, text: str) -> AIVerdict | None:
        from ..services.market import get_http_session

        session = await get_http_session()
        params = {
            "text": text[:2000],
            "lang": "fa",
            "mode": "standard",
            "api_user": settings.owner_id or 0,  # placeholder, unused for text-only
            "api_secret": settings.ai_api_key,
            "models": "toxicity,profanity,spam",
        }
        async with session.get("https://api.sightengine.com/1.0/text/check.json",
                               params=params, timeout=settings.ai_timeout) as response:
            if response.status != 200:
                return None
            data = await response.json(content_type=None)
        toxicity = 0.0
        try:
            toxicity = float((data.get("toxicity") or {}).get("overall", 0))
        except (TypeError, ValueError):
            toxicity = 0.0
        verdict = AIVerdict(score=max(toxicity, 0.0) / 100.0 if toxicity > 1 else toxicity,
                            categories={"toxicity": toxicity},
                            reason="تحلیل خودکار", provider="sightengine")
        return verdict


def _parse_verdict(content: str, provider: str) -> AIVerdict | None:
    try:
        start = content.find("{")
        end = content.rfind("}")
        payload = json.loads(content[start:end + 1]) if start >= 0 and end > start else {}
    except Exception:  # noqa: BLE001
        logger.debug("could not parse ai verdict: %s", content[:120])
        return None

    def _score(key: str) -> float:
        try:
            value = float(payload.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, value))

    categories = {key: _score(key) for key in ("toxicity", "harassment", "spam", "adult")}
    score = max(categories.values()) if categories else 0.0
    try:
        score = max(score, float(payload.get("score") or 0))
    except (TypeError, ValueError):
        pass
    return AIVerdict(score=score, categories=categories,
                     reason=str(payload.get("reason") or "")[:200], provider=provider)


ai_moderator = AIModerator()
