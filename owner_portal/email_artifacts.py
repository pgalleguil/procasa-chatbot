"""Immutable sent-email HTML lookup and surgical portal-link rewriting."""

from __future__ import annotations

import hashlib
import hmac
import gzip
import re
from html import escape, unescape
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit


EMAIL_ARTIFACT_COLLECTION = "owner_campaign_email_artifacts"
ALLOWED_SOURCES = frozenset({"EMAIL", "WHATSAPP"})
_ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r"(?P<prefix>\bhref\s*=\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)", re.IGNORECASE | re.DOTALL)
_RESPONSE_ACTIONS = frozenset({"aceptar_rebaja", "contactar_ejecutivo"})


def _href_action(value: str) -> str | None:
    parsed = urlsplit(unescape(value).strip())
    if parsed.path == "/campana/informe":
        return "ver_informe"
    if parsed.path == "/campana/respuesta":
        actions = parse_qs(parsed.query, keep_blank_values=True).get("accion", [])
        if len(actions) == 1 and actions[0] in _RESPONSE_ACTIONS:
            return actions[0]
        if actions:
            raise ValueError("unexpected_campaign_response_action")
    return None


def _rewrite_href_value(tag: str, href_match: re.Match[str], url: str) -> str:
    quote = href_match.group("quote")
    replacement = f"{href_match.group('prefix')}{quote}{escape(url, quote=True)}{quote}"
    return tag[: href_match.start()] + replacement + tag[href_match.end() :]


def transform_sent_email_to_portal_html(
    original_html: str,
    view: Mapping[str, Any],
    source: str,
) -> tuple[str, int]:
    """Change only the href values for the three campaign action types."""
    if not isinstance(original_html, str) or not original_html:
        raise ValueError("historical_email_html_missing")
    if source not in ALLOWED_SOURCES:
        raise ValueError("invalid_portal_source")

    urls = {
        "aceptar_rebaja": str(view.get("primary_url") or ""),
        "contactar_ejecutivo": str(view.get("advisor_url") or ""),
        "ver_informe": str(view.get("report_url") or ""),
    }
    if view.get("already_authorized") and not urls["aceptar_rebaja"]:
        # Keep the original anchor's visible markup while preventing another action.
        urls["aceptar_rebaja"] = "#ajuste-autorizado"

    counts = {action: 0 for action in urls}
    out: list[str] = []
    cursor = 0
    for tag_match in _ANCHOR_RE.finditer(original_html):
        out.append(original_html[cursor : tag_match.start()])
        tag = tag_match.group(0)
        href_match = _HREF_RE.search(tag)
        if href_match:
            action = _href_action(href_match.group("value"))
            if action:
                target = urls[action]
                if not target:
                    raise ValueError(f"portal_action_url_missing:{action}")
                tag = _rewrite_href_value(tag, href_match, target)
                counts[action] += 1
        out.append(tag)
        cursor = tag_match.end()
    out.append(original_html[cursor:])

    if counts["aceptar_rebaja"] < 1 or counts["contactar_ejecutivo"] < 1:
        raise ValueError("historical_email_cta_missing")
    if bool(view.get("document_available")) != (counts["ver_informe"] > 0):
        raise ValueError("historical_email_report_link_mismatch")
    return "".join(out), sum(counts.values())


def mask_authorized_hrefs(document: str) -> str:
    """Mask only recognized campaign href values, preserving every other byte."""
    out: list[str] = []
    cursor = 0
    for tag_match in _ANCHOR_RE.finditer(document):
        out.append(document[cursor : tag_match.start()])
        tag = tag_match.group(0)
        href_match = _HREF_RE.search(tag)
        href_value = unescape(href_match.group("value")).strip() if href_match else ""
        if href_match and (_href_action(href_value) or href_value == "#ajuste-autorizado"):
            quote = href_match.group("quote")
            marker = f"{href_match.group('prefix')}{quote}__AUTHORIZED_CAMPAIGN_HREF__{quote}"
            tag = tag[: href_match.start()] + marker + tag[href_match.end() :]
        out.append(tag)
        cursor = tag_match.end()
    out.append(document[cursor:])
    return "".join(out)


def verify_original_email_artifact(artifact: Mapping[str, Any]) -> str:
    """Return stored source HTML only when its exact UTF-8 bytes and digest agree."""
    compressed = artifact.get("html_gzip")
    if not isinstance(compressed, (bytes, bytearray, memoryview)) or not compressed:
        raise ValueError("historical_email_html_missing")
    try:
        encoded = gzip.decompress(bytes(compressed))
        original = encoded.decode(str(artifact.get("html_encoding") or "utf-8"))
    except (OSError, EOFError, UnicodeDecodeError, LookupError) as exc:
        raise ValueError("historical_email_artifact_integrity_error") from exc
    if not original:
        raise ValueError("historical_email_html_missing")
    expected_bytes = artifact.get("original_html_bytes")
    expected_hash = str(artifact.get("original_html_sha256") or "")
    actual_hash = hashlib.sha256(encoded).hexdigest()
    if expected_bytes != len(encoded) or not hmac.compare_digest(expected_hash, actual_hash):
        raise ValueError("historical_email_artifact_integrity_error")
    return original


def masked_html_parity(original_html: str, portal_html: str) -> bool:
    return mask_authorized_hrefs(original_html) == mask_authorized_hrefs(portal_html)
