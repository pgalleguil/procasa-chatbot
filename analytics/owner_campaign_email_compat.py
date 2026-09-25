"""Email-client compatibility transforms for the OWNER_CAMPAIGN_EMAIL_V2 HTML."""

from __future__ import annotations

import re

from lxml import html
from premailer import transform

_MEDIA_START_RE = re.compile(r"@media[^{}]+\{")


def _protect_media_overrides(source_html: str) -> str:
    """Keep authored mobile overrides effective after desktop CSS is inlined."""
    pieces: list[str] = []
    cursor = 0
    for match in _MEDIA_START_RE.finditer(source_html):
        depth = 1
        close = match.end()
        while close < len(source_html) and depth:
            if source_html[close] == "{":
                depth += 1
            elif source_html[close] == "}":
                depth -= 1
            close += 1
        if depth:
            continue
        body_start, body_end = match.end(), close - 1
        body = source_html[body_start:body_end]
        body = re.sub(
            r"\{([^{}]*)\}",
            lambda rule: "{" + ";".join(
                declaration if ":" not in declaration or "!important" in declaration.casefold()
                else declaration.rstrip() + " !important"
                for declaration in rule.group(1).split(";")
            ) + "}",
            body,
            flags=re.DOTALL,
        )
        pieces.extend((source_html[cursor:body_start], body))
        cursor = body_end
    if pieces:
        pieces.append(source_html[cursor:])
        return "".join(pieces)
    return source_html


def make_email_safe_html(source_html: str) -> str:
    """Inline critical class-based CSS while retaining responsive enhancements."""
    normalized_source = re.sub(r"\s*!important\b", "", source_html, flags=re.IGNORECASE)
    responsive_source = _protect_media_overrides(normalized_source)
    inlined_html = transform(
        responsive_source,
        keep_style_tags=True,
        remove_classes=False,
        strip_important=False,
        disable_validation=True,
        allow_network=False,
    )
    source_tree = html.fromstring(source_html)
    final_tree = html.fromstring(inlined_html)
    source_nodes = list(source_tree.iter())
    final_nodes = list(final_tree.iter())
    if len(source_nodes) != len(final_nodes):
        raise ValueError("Email CSS inlining changed the HTML element structure")
    for source_node, final_node in zip(source_nodes, final_nodes):
        if source_node.get("width") is None and final_node.get("width") is not None:
            if final_node.tag not in {"table", "img"}:
                final_node.attrib.pop("width", None)
        if source_node.get("height") is None and final_node.get("height") is not None:
            if final_node.tag not in {"table", "img", "td", "th", "tr"}:
                final_node.attrib.pop("height", None)
    return html.tostring(final_tree, encoding="unicode", method="html", doctype="<!doctype html>")
