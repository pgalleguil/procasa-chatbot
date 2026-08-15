"""HTML storage and reuse system for property dumps."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import HTML_DUMPS_DIR


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def md5_url(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def html_path(url: str, batch_id: str) -> Path:
    fid = md5_url(url)
    return HTML_DUMPS_DIR / batch_id / f"{fid}.html"


def metadata_path(url: str, batch_id: str) -> Path:
    fid = md5_url(url)
    return HTML_DUMPS_DIR / batch_id / f"{fid}.json"


def metadata_exists(url: str, batch_id: str) -> bool:
    return metadata_path(url, batch_id).exists()


def load_metadata(url: str, batch_id: str) -> dict[str, Any] | None:
    p = metadata_path(url, batch_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_html(url: str, batch_id: str) -> str | None:
    p = html_path(url, batch_id)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def save_html(url: str, html: str, batch_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    hpath = html_path(url, batch_id)
    hpath.parent.mkdir(parents=True, exist_ok=True)
    hpath.write_text(html, encoding="utf-8")

    h256 = sha256_text(html)
    metadata = {
        "listing_id": meta.get("listing_id", ""),
        "url": url,
        "canonical_url": meta.get("canonical_url", url),
        "downloaded_at": _utcnow(),
        "download_method": meta.get("fetch_source", "http"),
        "http_status": meta.get("status_code", 200),
        "content_encoding": meta.get("content_encoding", ""),
        "wire_bytes": meta.get("wire_bytes", len(html.encode("utf-8"))),
        "html_bytes": len(html.encode("utf-8")),
        "html_validation_status": meta.get("validation_status", ""),
        "sha256_html": h256,
        "proxy_used": meta.get("proxy_used", False),
        "proxy_session_id": meta.get("proxy_session_id", ""),
        "batch_id": batch_id,
    }
    mpath = metadata_path(url, batch_id)
    mpath.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def is_valid_local_html(url: str, batch_id: str) -> tuple[bool, str | None]:
    meta = load_metadata(url, batch_id)
    if not meta:
        return False, "no_metadata"
    html = load_html(url, batch_id)
    if not html:
        return False, "no_html_file"
    h256 = sha256_text(html)
    if meta.get("sha256_html") and meta["sha256_html"] != h256:
        return False, "hash_mismatch"
    if meta.get("html_validation_status") in ("OK", "LISTING_REMOVED"):
        return True, meta["html_validation_status"]
    return False, f"invalid_status:{meta.get('html_validation_status','unknown')}"
