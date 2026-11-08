"""Offline nudity detection for Telegram photos, animations and videos.

The NudeNet model runs locally through ONNX Runtime; media is not sent to a
third-party moderation service. For moving media, a small set of frames is
sampled to keep CPU usage bounded. This is a best-effort detector, not a promise
of perfect recall.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from aiogram import Bot
from aiogram.types import Message

logger = logging.getLogger("armando.media_safety")

# The hosted Telegram Bot API limits getFile downloads to 20 MB. Leave headroom
# below that hard ceiling and skip oversized items rather than buffering them.
MAX_DOWNLOAD_BYTES = 19 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_SAMPLED_FRAMES = 8

IMAGE_EXTENSIONS = frozenset({".avif", ".bmp", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"})
VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"})

# Only explicit exposed anatomy is acted on. Covered areas, feet, belly and
# armpits are intentionally excluded to reduce false positives.
EXPLICIT_THRESHOLDS: dict[str, float] = {
    "FEMALE_GENITALIA_EXPOSED": 0.50,
    "MALE_GENITALIA_EXPOSED": 0.50,
    "ANUS_EXPOSED": 0.55,
    "FEMALE_BREAST_EXPOSED": 0.62,
    "BUTTOCKS_EXPOSED": 0.70,
}


@dataclass(frozen=True)
class _MediaRef:
    file_id: str
    kind: str  # image | gif | video
    file_size: int | None = None
    file_name: str | None = None
    duration: int | None = None


_detector: Any = None
_detector_lock = threading.Lock()
_inference_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="armando-nsfw")


def _media_ref(message: Message) -> _MediaRef | None:
    """Pick only visual media that can be inspected safely."""
    photos = getattr(message, "photo", None) or []
    if photos:
        photo = max(photos, key=lambda item: int(getattr(item, "file_size", 0) or 0))
        return _MediaRef(file_id=photo.file_id, kind="image",
                         file_size=getattr(photo, "file_size", None))

    animation = getattr(message, "animation", None)
    if animation is not None:
        name = getattr(animation, "file_name", None)
        suffix = Path(name or "").suffix.lower()
        return _MediaRef(file_id=animation.file_id,
                         kind="gif" if suffix == ".gif" else "video",
                         file_size=getattr(animation, "file_size", None),
                         file_name=name,
                         duration=getattr(animation, "duration", None))

    video = getattr(message, "video", None)
    if video is not None:
        return _MediaRef(file_id=video.file_id, kind="video",
                         file_size=getattr(video, "file_size", None),
                         file_name=getattr(video, "file_name", None),
                         duration=getattr(video, "duration", None))

    video_note = getattr(message, "video_note", None)
    if video_note is not None:
        return _MediaRef(file_id=video_note.file_id, kind="video",
                         file_size=getattr(video_note, "file_size", None),
                         duration=getattr(video_note, "duration", None))

    document = getattr(message, "document", None)
    if document is not None:
        name = getattr(document, "file_name", None)
        suffix = Path(name or "").suffix.lower()
        mime = (getattr(document, "mime_type", None) or "").lower()
        if suffix == ".gif":
            kind = "gif"
        elif mime.startswith("image/") or suffix in IMAGE_EXTENSIONS:
            kind = "image"
        elif mime.startswith("video/") or suffix in VIDEO_EXTENSIONS:
            kind = "video"
        else:
            return None
        return _MediaRef(file_id=document.file_id, kind=kind,
                         file_size=getattr(document, "file_size", None),
                         file_name=name)
    return None


async def _download_media(bot: Bot, media: _MediaRef) -> bytes | None:
    """Download a bounded file through Telegram, keeping it only in memory."""
    if media.file_size is not None and media.file_size > MAX_DOWNLOAD_BYTES:
        logger.info("بررسی تصویری به‌دلیل بزرگی فایل (%s بایت) انجام نشد", media.file_size)
        return None
    try:
        remote = await bot.get_file(media.file_id)
        file_path = getattr(remote, "file_path", None)
        size = getattr(remote, "file_size", None)
        if size is not None and size > MAX_DOWNLOAD_BYTES:
            logger.info("بررسی تصویری به‌دلیل بزرگی فایل تلگرام (%s بایت) انجام نشد", size)
            return None
        if not file_path:
            return None
        buffer = io.BytesIO()
        await bot.download_file(file_path, destination=buffer)
        payload = buffer.getvalue()
        if len(payload) > MAX_DOWNLOAD_BYTES:
            logger.info("بررسی تصویری به‌دلیل بزرگی فایل دریافت‌شده (%s بایت) انجام نشد",
                        len(payload))
            return None
        return payload
    except Exception as exc:  # noqa: BLE001 - moderation must never break message handling
        logger.warning("دریافت رسانه برای بررسی تصویری ناموفق بود (%s)", exc.__class__.__name__)
        return None


def _get_detector():
    """Load the bundled CPU model once, on demand."""
    global _detector
    if _detector is None:
        with _detector_lock:
            if _detector is None:
                from nudenet import NudeDetector

                _detector = NudeDetector()
                logger.info("مدل محلی NudeNet برای بررسی تصویر آماده است")
    return _detector


def _contains_explicit(detections: list[dict[str, Any]] | None) -> bool:
    for item in detections or []:
        label = str(item.get("class", "")).upper()
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        threshold = EXPLICIT_THRESHOLDS.get(label)
        if threshold is not None and score >= threshold:
            return True
    return False


def _sample_indices(total: int, limit: int = MAX_SAMPLED_FRAMES) -> list[int]:
    """Return evenly spaced, unique frame indexes (including both endpoints)."""
    if total <= 0 or limit <= 0:
        return []
    if total <= limit:
        return list(range(total))
    if limit == 1:
        return [total // 2]
    return sorted({round(index * (total - 1) / (limit - 1)) for index in range(limit)})


def _detect_image(detector, payload: bytes) -> bool:
    from PIL import Image

    with Image.open(io.BytesIO(payload)) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS:
            logger.info("بررسی تصویری عکس با ابعاد بیش‌ازحد انجام نشد")
            return False
        frame = np.asarray(image.convert("RGB"))
    bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    return _contains_explicit(detector.detect(bgr))


def _detect_gif(detector, payload: bytes) -> bool:
    from PIL import Image

    with Image.open(io.BytesIO(payload)) as image:
        total = max(1, int(getattr(image, "n_frames", 1) or 1))
        if image.width * image.height > MAX_IMAGE_PIXELS:
            logger.info("بررسی تصویری گیف با ابعاد بیش‌ازحد انجام نشد")
            return False
        for index in _sample_indices(total):
            image.seek(index)
            frame = np.asarray(image.convert("RGB"))
            bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            if _contains_explicit(detector.detect(bgr)):
                return True
    return False


def _detect_video(detector, payload: bytes, file_name: str | None,
                  duration: int | None) -> bool:
    suffix = Path(file_name or "").suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        suffix = ".mp4"

    # OpenCV video decoders need a path. The temporary file is removed as soon
    # as the sampled frames have been inspected; no user media is retained.
    with tempfile.TemporaryDirectory(prefix="armando-media-") as temp_dir:
        path = Path(temp_dir) / f"input{suffix}"
        path.write_bytes(payload)
        capture = cv2.VideoCapture(str(path))
        try:
            total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            indexes = _sample_indices(total)
            if indexes:
                for index in indexes:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                    ok, frame = capture.read()
                    if ok and _contains_explicit(detector.detect(frame)):
                        return True
            elif duration:
                # Some container formats do not report a frame count. Seek at
                # evenly spaced times as a fallback.
                moments = _sample_indices(max(1, int(duration)), min(duration, MAX_SAMPLED_FRAMES))
                for second in moments:
                    capture.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
                    ok, frame = capture.read()
                    if ok and _contains_explicit(detector.detect(frame)):
                        return True
        finally:
            capture.release()
    return False


def _detect_media_sync(payload: bytes, media: _MediaRef) -> bool:
    detector = _get_detector()
    if media.kind == "image":
        return _detect_image(detector, payload)
    if media.kind == "gif":
        return _detect_gif(detector, payload)
    if media.kind == "video":
        return _detect_video(detector, payload, media.file_name, media.duration)
    return False


async def detect_visual_nudity(bot: Bot, message: Message) -> bool:
    """Inspect supported visual media; return True only for confident nudity.

    Oversized files, unsupported formats, download failures, or model failures
    are skipped safely. The moderation pipeline can still inspect captions and
    filenames, and a detector problem never crashes the bot.
    """
    media = _media_ref(message)
    if media is None:
        return False
    if media.file_size is not None and media.file_size > MAX_DOWNLOAD_BYTES:
        logger.info("بررسی تصویری به‌دلیل بزرگی فایل (%s بایت) انجام نشد", media.file_size)
        return False
    payload = await _download_media(bot, media)
    if not payload:
        return False
    try:
        loop = asyncio.get_running_loop()
        return bool(await loop.run_in_executor(_inference_pool, _detect_media_sync,
                                               payload, media))
    except Exception as exc:  # noqa: BLE001 - a bad model/media item cannot stop moderation
        logger.warning("بررسی برهنگی تصویر نادیده گرفته شد (%s)", exc.__class__.__name__)
        return False
