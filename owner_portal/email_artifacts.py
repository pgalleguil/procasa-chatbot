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
_TABLE_TAG_RE = re.compile(r"<table\b[^>]*>|</table\s*>", re.IGNORECASE | re.DOTALL)
_VALUATION_CLASS_RE = re.compile(r"\bclass\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
_PORTAL_UI_BLOCK_RE = re.compile(
    r"<!-- OWNER_PORTAL_UI:(CTA_TOP|STICKY_ACTION_BAR):START -->.*?"
    r"<!-- OWNER_PORTAL_UI:\1:END -->",
    re.IGNORECASE | re.DOTALL,
)
_PORTAL_UI_STYLE_RE = re.compile(
    r"<style\b(?=[^>]*\bid\s*=\s*([\"'])owner-portal-conversion-ui\1)[^>]*>.*?</style\s*>",
    re.IGNORECASE | re.DOTALL,
)
_PORTAL_UI_CSS = """<style id="owner-portal-conversion-ui">
  html { scroll-padding-bottom: calc(78px + env(safe-area-inset-bottom)); }
  body { padding-bottom: calc(78px + env(safe-area-inset-bottom)) !important; }
  .owner-portal-cta-top { box-sizing: border-box; width: 100%; max-width: 640px; margin: 14px auto 18px; padding: 0 16px; }
  .owner-portal-cta-top__action, .owner-portal-sticky__action { box-sizing: border-box; min-height: 50px; display: flex; align-items: center; justify-content: center; gap: 10px; padding: 13px 18px; border: 1px solid #1d1a63; border-radius: 13px; background: #1d1a63; color: #fff !important; font: 700 15px/1.25 Arial, Helvetica, sans-serif; text-align: center; text-decoration: none !important; }
  .owner-portal-cta-top__action { max-width: 600px; margin: 0 auto; }
  .owner-portal-cta-top__action:focus-visible, .owner-portal-sticky__action:focus-visible { outline: 3px solid #7c70ed; outline-offset: 3px; }
  .owner-portal-cta-top__action--disabled, .owner-portal-sticky__action--disabled { border-color: #aaa9c9; background: #eeedfa; color: #575577 !important; cursor: default; }
  .owner-portal-sticky { box-sizing: border-box; position: fixed; z-index: 2147483000; left: max(10px, env(safe-area-inset-left)); right: max(10px, env(safe-area-inset-right)); bottom: 0; display: flex; align-items: stretch; gap: 7px; padding: 7px 8px calc(7px + env(safe-area-inset-bottom)); border: 1px solid #deddf0; border-bottom: 0; border-radius: 17px 17px 0 0; background: rgba(255,255,255,.98); box-shadow: 0 -5px 22px rgba(24,22,73,.12); visibility: hidden; }
  .owner-portal-sticky.is-visible { visibility: visible; }
  .owner-portal-sticky__action { flex: 1 1 50%; min-width: 0; min-height: 44px; padding: 8px; font-size: 13px; }
  .owner-portal-sticky__advisor { border-color: #c8c5ed; background: #f5f4ff; color: #24206c !important; }
  @media (min-width: 720px) {
    body { padding-bottom: 94px !important; }
    .owner-portal-sticky { left: 50%; right: auto; bottom: 18px; width: min(672px, calc(100vw - 32px)); padding: 10px; border: 1px solid #deddf0; border-radius: 16px; transform: translateX(-50%); }
  }
  @media (prefers-reduced-motion: no-preference) {
    .owner-portal-sticky { animation: owner-portal-enter .2s ease-out both; }
    @keyframes owner-portal-enter { from { opacity: .82; } to { opacity: 1; } }
  }
  @media (prefers-reduced-motion: reduce) { .owner-portal-sticky { animation: none !important; } }
</style>
""".strip()


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


def _valuation_strip_end(document: str) -> int:
    matches = []
    for tag_match in _TABLE_TAG_RE.finditer(document):
        tag = tag_match.group(0)
        if not tag.lower().startswith("<table"):
            continue
        class_match = _VALUATION_CLASS_RE.search(tag)
        if class_match and "valuation-strip-single" in class_match.group(2).split():
            matches.append(tag_match)
    if len(matches) != 1:
        raise ValueError("historical_email_valuation_strip_missing_or_ambiguous")

    depth = 1
    for tag_match in _TABLE_TAG_RE.finditer(document, matches[0].end()):
        if tag_match.group(0).lower().startswith("</table"):
            depth -= 1
            if depth == 0:
                return tag_match.end()
        else:
            depth += 1
    raise ValueError("historical_email_valuation_strip_unclosed")


def _button_markup(url: str, label: str, *, classes: str, disabled_label: str = "") -> str:
    if url:
        return (
            f'<a class="{classes}" href="{escape(url, quote=True)}">'
            f'{escape(label)}<span aria-hidden="true">→</span></a>'
        )
    text = disabled_label or label
    return f'<span class="{classes} {classes.split()[0]}--disabled" aria-disabled="true">{escape(text)}</span>'


def inject_owner_portal_controls(
    document: str, view: Mapping[str, Any], *, include_top_cta: bool = True,
) -> str:
    if "OWNER_PORTAL_UI:" in document or "owner-portal-conversion-ui" in document:
        raise ValueError("owner_portal_conversion_ui_already_present")
    primary_disabled = "Ajuste autorizado" if view.get("already_authorized") else "Ajuste en revisión"
    if include_top_cta:
        top_button = _button_markup(
            str(view.get("top_primary_url") or ""),
            "REVISAR / CONFIRMAR AJUSTE",
            classes="owner-portal-cta-top__action",
            disabled_label=primary_disabled,
        )
        top_block = (
            "<!-- OWNER_PORTAL_UI:CTA_TOP:START -->"
            f'<div class="owner-portal-cta-top" data-owner-portal-ui="CTA_TOP">{top_button}</div>'
            "<!-- OWNER_PORTAL_UI:CTA_TOP:END -->"
        )
        top_end = _valuation_strip_end(document)
        document = document[:top_end] + top_block + document[top_end:]

    style_matches = list(re.finditer(r"</head\s*>", document, re.IGNORECASE))
    if len(style_matches) != 1:
        raise ValueError("historical_email_head_missing_or_ambiguous")
    close_head = style_matches[0]
    document = document[: close_head.start()] + _PORTAL_UI_CSS + document[close_head.start() :]

    sticky_primary = _button_markup(
        str(view.get("sticky_primary_url") or ""),
        "Revisar ajuste",
        classes="owner-portal-sticky__action owner-portal-sticky__primary",
        disabled_label=primary_disabled,
    )
    advisor_url = str(view.get("sticky_advisor_url") or view.get("advisor_url") or "")
    if not advisor_url:
        raise ValueError("portal_sticky_advisor_url_missing")
    sticky_advisor = _button_markup(
        advisor_url, "Hablar con ejecutivo",
        classes="owner-portal-sticky__action owner-portal-sticky__advisor",
    )
    sticky_block = (
        "<!-- OWNER_PORTAL_UI:STICKY_ACTION_BAR:START -->"
        '<nav class="owner-portal-sticky" data-owner-portal-ui="STICKY_ACTION_BAR" '
        'aria-label="Acciones de tu propiedad">'
        f"{sticky_primary}{sticky_advisor}</nav>"
    '<script>(function(){var bar=document.querySelector(".owner-portal-sticky");'
    'var top=document.querySelector(".owner-portal-cta-top");if(!bar)return;'
    'if(!top||!window.IntersectionObserver){bar.classList.add("is-visible");return;}'
    'var observer=new IntersectionObserver(function(entries){bar.classList.toggle("is-visible",!entries[0].isIntersecting);},{threshold:0.01});'
    'observer.observe(top);})();</script>'
        "<!-- OWNER_PORTAL_UI:STICKY_ACTION_BAR:END -->"
    )
    body_closes = list(re.finditer(r"</body\s*>", document, re.IGNORECASE))
    if len(body_closes) != 1:
        raise ValueError("historical_email_body_missing_or_ambiguous")
    close_body = body_closes[0]
    return document[: close_body.start()] + sticky_block + document[close_body.start() :]


def transform_sent_email_to_portal_html(
    original_html: str,
    view: Mapping[str, Any],
    source: str,
) -> tuple[str, int]:
    """Rewrite campaign links and add Owner Portal-only conversion controls."""
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
    rewritten = "".join(out)
    return inject_owner_portal_controls(rewritten, view, include_top_cta=True), sum(counts.values())


def mask_authorized_hrefs(document: str) -> str:
    """Mask permitted CTA hrefs and remove only explicitly marked portal UI."""
    document = _PORTAL_UI_BLOCK_RE.sub("", document)
    document = _PORTAL_UI_STYLE_RE.sub("", document)
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
