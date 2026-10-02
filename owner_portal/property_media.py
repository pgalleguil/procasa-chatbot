"""Verified public property media for monthly owner portal snapshots.

Network access belongs to the trusted preparation process. Portal requests only
read the already verified metadata persisted in the monthly snapshot.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import urlsplit


PUBLIC_PROPERTY_BASE = "https://www.procasa.cl"
_ALLOWED_IMAGE_HOSTS = {"demoazimg.prop360.cl", "img.prop360.cl", "www.procasa.cl", "procasa.cl"}


class _ListingMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical: list[str] = []
        self.open_graph_url: list[str] = []
        self.open_graph_images: list[str] = []
        self.image_sources: list[str] = []
        self.body_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "link" and "canonical" in values.get("rel", "").casefold().split():
            self.canonical.append(values.get("href", ""))
        if tag.casefold() == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            if key == "og:url":
                self.open_graph_url.append(values.get("content", ""))
            elif key == "og:image":
                self.open_graph_images.append(values.get("content", ""))
        elif tag.casefold() == "img" and values.get("src"):
            self.image_sources.append(values["src"])

    def handle_data(self, data: str) -> None:
        self.body_text.append(data)


def _property_url_matches(url: str, property_code: str) -> bool:
    parsed = urlsplit(str(url or "").strip())
    path = parsed.path.rstrip("/")
    return (
        parsed.scheme.casefold() == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold() in {"procasa.cl", "www.procasa.cl"}
        and path == f"/{property_code}"
        and not parsed.username
        and not parsed.password
    )


def _valid_image_url(value: str) -> bool:
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").casefold()
    return (
        parsed.scheme.casefold() == "https"
        and host in _ALLOWED_IMAGE_HOSTS
        and bool(parsed.path)
        and not parsed.username
        and not parsed.password
    )


def parse_public_property_page(html: str, *, property_code: Any) -> dict[str, Any]:
    """Extract primary photo only when canonical/OG identity matches the code."""
    code = str(property_code or "").strip()
    if not code or not isinstance(html, str) or not html.strip():
        return {"status": "MISSING", "property_code_match": False, "hero_image_url": None}
    parser = _ListingMetadata()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return {"status": "ERROR", "property_code_match": False, "hero_image_url": None}

    page_urls = [item for item in parser.open_graph_url + parser.canonical if item]
    matching_urls = [item for item in page_urls if _property_url_matches(item, code)]
    if not matching_urls or any(not _property_url_matches(item, code) for item in page_urls):
        return {"status": "IDENTITY_MISMATCH", "property_code_match": False, "hero_image_url": None}
    primary = next((item.strip() for item in parser.open_graph_images if _valid_image_url(item)), None)
    return {
        "status": "FOUND" if primary else "IMAGE_MISSING",
        "property_code_match": True,
        "public_page_url": f"{PUBLIC_PROPERTY_BASE}/{code}",
        "hero_image_url": primary,
        "image_source": "PROCASA_PUBLIC_PROPERTY" if primary else None,
    }


def fetch_verified_property_media(property_code: Any, *, timeout: float = 15.0) -> dict[str, Any]:
    """Fetch one exact PROCASA listing and validate its primary image response.

    Intended for monthly preparation only; never call from portal request code.
    """
    import urllib.error
    import urllib.request

    code = str(property_code or "").strip()
    if not code or not code.isdigit():
        return {"status": "INVALID_CODE", "property_code_match": False, "hero_image_url": None}
    page_url = f"{PUBLIC_PROPERTY_BASE}/{code}"
    request = urllib.request.Request(
        page_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; PROCASA-OwnerPortal-Monthly/1.0)", "Accept": "text/html"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            final_url = response.geturl()
            page_html = response.read(3_000_000).decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": "PAGE_ERROR", "property_code_match": False, "hero_image_url": None,
                "error_type": type(exc).__name__}
    if status < 200 or status >= 300 or not _property_url_matches(final_url, code):
        return {"status": "PAGE_MISMATCH", "http_status": status, "property_code_match": False, "hero_image_url": None}
    result = parse_public_property_page(page_html, property_code=code)
    result["http_status"] = status
    if not result.get("property_code_match"):
        return result
    image_url = result.get("hero_image_url")
    if image_url:
        image_request = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0 (compatible; PROCASA-OwnerPortal-Monthly/1.0)", "Range": "bytes=0-511"})
        try:
            with urllib.request.urlopen(image_request, timeout=timeout) as response:
                content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().casefold()
                image_bytes = response.read(512)
                result["image_http_status"] = int(response.status)
                result["image_render_pass"] = 200 <= int(response.status) < 300 and content_type.startswith("image/") and bool(image_bytes)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            result["image_render_pass"] = False
            result["image_error_type"] = type(exc).__name__
    if result.get("status") == "FOUND" and not result.get("image_render_pass"):
        result["status"] = "IMAGE_ERROR"
    if result.get("status") == "FOUND":
        result["verified_property_code"] = code
        result["verified_at"] = datetime.now(timezone.utc)
        result["public_page_active"] = True
    return result


def verified_media_for_property(
    property_code: Any,
    *,
    monthly: Mapping[str, Any] | None = None,
    row: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return existing verified same-property media metadata, without network."""
    code = str(property_code or "").strip()
    candidates: list[Mapping[str, Any]] = []
    if isinstance(monthly, Mapping):
        candidates.extend([
            monthly.get("property_media") if isinstance(monthly.get("property_media"), Mapping) else {},
            ((monthly.get("property") or {}).get("property_media"))
            if isinstance(monthly.get("property"), Mapping) and isinstance(monthly.get("property", {}).get("property_media"), Mapping) else {},
        ])
    if isinstance(row, Mapping):
        snapshot = row.get("campaign_snapshot") if isinstance(row.get("campaign_snapshot"), Mapping) else {}
        candidates.extend([
            snapshot.get("property_media") if isinstance(snapshot.get("property_media"), Mapping) else {},
            row.get("property_media") if isinstance(row.get("property_media"), Mapping) else {},
        ])
    for item in candidates:
        page_ok = _property_url_matches(str(item.get("public_page_url") or ""), code)
        if (str(item.get("verified_property_code") or "").strip() == code
                and item.get("image_source") in {"PROCASA_PUBLIC_PROPERTY", "EXISTING_PROPERTY_DATA", "HISTORICAL_PROPERTY_IMAGE"}
                and page_ok
                and _valid_image_url(str(item.get("hero_image_url") or ""))):
            return dict(item)
    return None


def verified_historical_media(email_html: str | None, property_code: Any) -> dict[str, Any] | None:
    """Accept a historical email image only when its asset filename binds to code."""
    import re
    from urllib.parse import unquote

    code = str(property_code or "").strip()
    if not code or not isinstance(email_html, str) or not email_html.strip():
        return None
    parser = _ListingMetadata()
    try:
        parser.feed(email_html)
        parser.close()
    except Exception:
        return None
    for value in parser.image_sources:
        if not _valid_image_url(value):
            continue
        filename = unquote(urlsplit(value).path.rsplit("/", 1)[-1])
        if re.match(rf"^{re.escape(code)}(?:[_-]|\.)", filename, re.IGNORECASE):
            return {
                "public_page_url": f"{PUBLIC_PROPERTY_BASE}/{code}",
                "hero_image_url": value,
                "image_source": "HISTORICAL_PROPERTY_IMAGE",
                "verified_property_code": code,
            }
    return None


def verified_media_from_property_record(record: Mapping[str, Any] | None, property_code: Any) -> dict[str, Any] | None:
    """Extract an image only from a property record whose own code matches."""
    code = str(property_code or "").strip()
    if not isinstance(record, Mapping) or not code:
        return None
    record_code = str(record.get("codigo") or record.get("property_code") or record.get("listing_id") or "").strip()
    if record_code != code:
        return None
    candidates: list[Any] = []
    for key in ("main_image_url", "image_url", "photo_url", "foto_principal", "imagen_principal"):
        if record.get(key):
            candidates.append(record[key])
    for key in ("image_urls", "images", "photos", "fotos", "imagenes"):
        value = record.get(key)
        if isinstance(value, (list, tuple)):
            candidates.extend(value)
        elif value:
            candidates.append(value)
    for value in candidates:
        if isinstance(value, Mapping):
            value = value.get("url") or value.get("src") or value.get("image_url")
        if isinstance(value, str) and _valid_image_url(value):
            return {
                "public_page_url": f"{PUBLIC_PROPERTY_BASE}/{code}",
                "hero_image_url": value,
                "image_source": "EXISTING_PROPERTY_DATA",
                "verified_property_code": code,
            }
    return None
