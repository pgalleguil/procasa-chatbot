"""visual_watermark_detector.py

Optional visual watermark / logo detector for property listing images.

Safe-fail design:
  - If pytesseract or Pillow are not installed, returns a degraded result
    with visual_watermark_error = "ocr_not_available".
  - If any image download or OCR step fails, the error is captured per-image
    and does not abort the scraper.
  - The absence of watermark text is NEVER used as a dueño signal.

Usage (from classify_seller_state):
    from visual_watermark_detector import detect_watermarks
    result = detect_watermarks(image_urls, max_images=3, timeout=8)
    if result["visual_watermark_broker_score"] >= 70:
        # override to CORREDOR_SEGURO
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("yapo_scraper.visual")

# Keywords that strongly indicate a real-estate intermediary
BROKER_VISUAL_KEYWORDS: tuple[str, ...] = (
    "propiedades",
    "broker",
    "corredores",
    "corredora",
    "corredor",
    "inmobiliaria",
    "spa",
    "ltda",
    "eirl",
    "real estate",
    "bienes raices",
    "corretaje",
    "agente inmobiliario",
    "remax",
    "re/max",
    "century 21",
    "coldwell",
    "kw chile",
    "keller williams",
)

# URL fragments that indicate site assets, not property photos
_ASSET_URL_FRAGMENTS: tuple[str, ...] = (
    "/static/",
    "/assets/",
    "/img/ui/",
    "/icons/",
    "/logo",
    "favicon",
    "placeholder",
    "spinner",
    "avatar",
    "user_way",
    "userway",
    "accessibility",
    "tracking",
    "pixel.gif",
    "pixel.png",
    "blank.gif",
    "transparent",
)

_YAPO_DOMAIN_ASSETS = (
    "static.yapo.cl",
    "assets.yapo.cl",
    "fonts.gstatic.com",
    "fonts.googleapis.com",
)


def _is_property_image_url(url: str) -> bool:
    """Return True if the URL is likely a real property photo, not a UI asset."""
    url_lower = url.lower()
    for frag in _ASSET_URL_FRAGMENTS:
        if frag in url_lower:
            return False
    for domain in _YAPO_DOMAIN_ASSETS:
        if domain in url_lower:
            return False
    # Require the URL to end with a common image extension, or look like a CDN path
    if not re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", url_lower):
        if not re.search(r"/(img|image|photo|foto|cdn|t_or_|t_thumb|f_auto|v\d+)/", url_lower):
            if "photos.encuentra24.com" not in url_lower:
                return False
    return True


def _normalize_ocr_text(text: str) -> str:
    """Lowercase, strip accents, remove punctuation for robust keyword matching."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _score_text(text: str) -> tuple[int, list[str]]:
    """Return (score, matched_keywords) for a given OCR text block."""
    norm = _normalize_ocr_text(text)
    found: list[str] = []
    score = 0
    for kw in BROKER_VISUAL_KEYWORDS:
        kw_norm = _normalize_ocr_text(kw)
        if kw_norm and kw_norm in norm:
            if kw_norm not in [_normalize_ocr_text(f) for f in found]:
                found.append(kw)
                # Weight by specificity: longer / more specific keywords score higher
                score += min(30, max(10, len(kw_norm) * 3))
    return min(100, score), found


def _try_ocr_image(image_bytes: bytes) -> tuple[str, str]:
    """
    Attempt OCR on raw image bytes.
    Returns (ocr_text, error_message).
    error_message is empty string on success.
    """
    try:
        import io
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore
    except ImportError:
        return "", "ocr_not_available"

    try:
        image = Image.open(io.BytesIO(image_bytes))
        # Convert to RGB to avoid mode issues (e.g. CMYK, P)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        ocr_text = pytesseract.image_to_string(image, lang="spa+eng", timeout=10)
        return ocr_text, ""
    except Exception as exc:
        return "", f"ocr_error:{exc!s}"


def _download_image(url: str, timeout: int) -> tuple[bytes, str]:
    """
    Download image bytes from URL.
    Returns (image_bytes, error_message).
    """
    try:
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "image/webp,image/avif,image/*,*/*;q=0.8",
            },
        )
        with urlopen(req, timeout=timeout) as resp:
            # Refuse to read more than 5 MB per image
            return resp.read(5 * 1024 * 1024), ""
    except URLError as exc:
        return b"", f"download_error:{exc.reason}"
    except Exception as exc:
        return b"", f"download_error:{exc!s}"


def detect_watermarks(
    image_urls: list[str],
    *,
    max_images: int = 3,
    timeout: int = 8,
) -> dict[str, Any]:
    """
    Check the first `max_images` real property photos for broker watermarks.

    Returns a metadata dict:
        visual_watermark_checked: bool
        visual_watermark_text: str   (concatenated OCR output from all checked images)
        visual_watermark_signals: list[str]  (matched keywords)
        visual_watermark_broker_score: int  (0–100)
        visual_watermark_error: str  (empty on success)
        visual_images_checked: int
        visual_images_available: int
    """
    # Filter to real property image URLs
    filtered = [u for u in image_urls if _is_property_image_url(u)]

    result: dict[str, Any] = {
        "visual_watermark_checked": False,
        "visual_watermark_text": "",
        "visual_watermark_signals": [],
        "visual_watermark_broker_score": 0,
        "visual_watermark_error": "",
        "visual_images_checked": 0,
        "visual_images_available": len(filtered),
    }

    if not filtered:
        result["visual_watermark_error"] = "no_property_images"
        return result

    candidates = filtered[:max_images]
    all_text_parts: list[str] = []
    all_signals: list[str] = []
    max_score = 0
    errors: list[str] = []

    for url in candidates:
        image_bytes, dl_error = _download_image(url, timeout)
        if dl_error:
            errors.append(dl_error)
            LOGGER.debug("Visual watermark: download failed for %s — %s", url, dl_error)
            continue

        ocr_text, ocr_error = _try_ocr_image(image_bytes)
        if ocr_error == "ocr_not_available":
            result["visual_watermark_error"] = "ocr_not_available"
            LOGGER.debug("Visual watermark: OCR not available in this environment")
            return result
        if ocr_error:
            errors.append(ocr_error)
            LOGGER.debug("Visual watermark: OCR error for %s — %s", url, ocr_error)
            continue

        result["visual_images_checked"] += 1
        all_text_parts.append(ocr_text)

        img_score, img_signals = _score_text(ocr_text)
        if img_score > max_score:
            max_score = img_score
        for sig in img_signals:
            if sig not in all_signals:
                all_signals.append(sig)

        # Stop after successfully processing 1 image (unless more are strictly required)
        # The user requested to review only the first real image, falling back to 2nd if it fails.
        if result["visual_images_checked"] >= 1:
            break

    # Consolidate
    result["visual_watermark_checked"] = result["visual_images_checked"] > 0
    result["visual_watermark_text"] = " | ".join(all_text_parts)[:2000]
    result["visual_watermark_signals"] = all_signals
    result["visual_watermark_broker_score"] = max_score
    if errors and not result["visual_watermark_checked"]:
        result["visual_watermark_error"] = "; ".join(errors[:3])

    if all_signals:
        LOGGER.info(
            "Visual watermark detected signals=%s score=%d",
            all_signals,
            max_score,
        )

    return result
