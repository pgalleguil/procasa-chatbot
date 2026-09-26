"""Email-client compatibility transforms for the OWNER_CAMPAIGN_EMAIL_V2 HTML."""

from __future__ import annotations

import re

from lxml import html
from premailer import transform

_MEDIA_START_RE = re.compile(r"@media[^{}]+\{")
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_PSEUDO_RE = re.compile(r"::?(before|after)\s*$", re.IGNORECASE)


def _materialize_pseudo_elements(source_html: str) -> str:
    """Turn generated before/after decorations into ordinary HTML spans.

    Email clients inconsistently render generated CSS content. Replacing each
    generated decoration with a child span preserves the authored selector and
    declarations, so desktop and mobile geometry stay the same while Gmail can
    render the decoration as regular markup.
    """
    tree = html.fromstring(source_html)
    style_nodes = tree.xpath("//style")
    replacements: dict[tuple[str, str], list[str]] = {}

    for style_node in style_nodes:
        stylesheet = style_node.text or ""

        def replace_rule(match: re.Match[str]) -> str:
            selector_group, declarations = match.groups()
            rewritten: list[str] = []
            for selector in selector_group.split(","):
                selector = selector.strip()
                pseudo_match = _PSEUDO_RE.search(selector)
                if not pseudo_match:
                    rewritten.append(selector)
                    continue
                pseudo = pseudo_match.group(1).lower()
                base_selector = selector[:pseudo_match.start()].strip()
                if not base_selector:
                    rewritten.append(selector)
                    continue
                key = (base_selector, pseudo)
                replacements.setdefault(key, []).append(declarations)
                rewritten.append(f"{base_selector} > .email-pseudo-{pseudo}")
            return f"{', '.join(rewritten)} {{{declarations}}}"

        style_node.text = _CSS_RULE_RE.sub(replace_rule, stylesheet)

    for (selector, pseudo), declaration_blocks in replacements.items():
        try:
            targets = tree.cssselect(selector)
        except Exception:
            # Leave unsupported selectors unchanged rather than altering layout.
            for style_node in style_nodes:
                css = style_node.text or ""
                css = css.replace(f"{selector} > .email-pseudo-{pseudo}", f"{selector}:{pseudo}")
                style_node.text = css
            continue
        for target in targets:
            if any(f"email-pseudo-{pseudo}" in (child.get("class") or "").split() for child in target):
                continue
            child = html.Element("span")
            child.set("class", f"email-pseudo-{pseudo}")
            child.set("aria-hidden", "true")
            blocks = declaration_blocks
            if any(re.search(r"\bcontent\s*:\s*none\b", block, re.IGNORECASE) for block in blocks):
                child.set("style", "display:none")
            target.append(child)

    return html.tostring(tree, encoding="unicode", method="html", doctype="<!doctype html>")


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
    materialized_source = _materialize_pseudo_elements(source_html)
    normalized_source = re.sub(r"\s*!important\b", "", materialized_source, flags=re.IGNORECASE)
    responsive_source = _protect_media_overrides(normalized_source)
    inlined_html = transform(
        responsive_source,
        keep_style_tags=True,
        remove_classes=False,
        strip_important=False,
        disable_validation=True,
        allow_network=False,
    )
    source_tree = html.fromstring(materialized_source)
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
