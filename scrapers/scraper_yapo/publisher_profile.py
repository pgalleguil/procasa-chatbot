"""Bounded, seller-profile-ID based Yapo history collection through FlashProxy only."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

try:
    from .downloader import DownloadResult, download_html
    from .proxy_manager import ProxyManager
except ImportError:  # direct script execution
    from downloader import DownloadResult, download_html
    from proxy_manager import ProxyManager

_DETAIL_LINK_RE = re.compile(r"/bienes-raices-(?:venta|alquiler)-[^/?#]+/[^/?#]+/(\d{6,})(?:[/?#]|$)", re.I)
_RELATED_RE = re.compile(r"related|recomend|similar|sugerid|carousel|sponsored", re.I)
_CARD_RE = re.compile(r"ad|tile|listing|card|publication|publicacion", re.I)
_PROFILE_PATH_RE = re.compile(r"^/user/profile/id/(\d{5,})/?$")
_COMMERCIAL_CUE_RE = re.compile(r"\b(?:corretaje|corredor(?:a)?|inmobiliaria|honorarios|comision(?:es)?)\b", re.I)


def _profile_url(profile_id: str, candidate_url: str = "") -> str:
    candidate = urljoin("https://www.yapo.cl/", candidate_url or f"/user/profile/id/{profile_id}")
    parsed = urlparse(candidate)
    match = _PROFILE_PATH_RE.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.hostname not in {"www.yapo.cl", "yapo.cl"}:
        raise ValueError("seller_profile_url must be an HTTPS Yapo profile URL")
    if not match or match.group(1) != str(profile_id):
        raise ValueError("seller_profile_url does not match seller_profile_id")
    return candidate


def _primary_root(soup: BeautifulSoup):
    root = soup.find("main") or soup.select_one('[data-testid*="profile"]') or soup.body or soup
    for node in list(root.find_all(True)):
        if node.attrs is None:  # an earlier related container may have decomposed this node
            continue
        attrs = " ".join((
            str(node.get("id") or ""),
            " ".join(str(value) for value in (node.get("class") or [])),
            str(node.get("data-testid") or ""),
        ))
        if node is not root and _RELATED_RE.search(attrs):
            node.decompose()
    return root


def _card_for_anchor(anchor, root):
    node = anchor
    for _ in range(6):
        if node is None or node is root:
            break
        attrs = " ".join((
            str(node.get("id") or ""),
            " ".join(str(value) for value in (node.get("class") or [])),
            str(node.get("data-testid") or ""),
        ))
        if node.name in {"article", "li"} or _CARD_RE.search(attrs):
            return node
        node = node.parent
    return anchor.parent or anchor


def _operation_and_type(url: str) -> tuple[str, str]:
    path = urlparse(url).path.lower()
    operation = "arriendo" if "alquiler" in path else "venta" if "venta" in path else "desconocido"
    category = path.split("bienes-raices-", 1)[-1].split("/", 1)[0]
    property_type = next((
        label for token, label in (
            ("casas", "casa"), ("departamentos", "departamento"),
            ("terrenos", "terreno"), ("parcelas", "parcela"),
            ("oficinas", "oficina"), ("locales", "local"),
        ) if token in category
    ), "desconocido")
    return operation, property_type


def parse_publisher_profile_page(
    source: str,
    *,
    seller_profile_id: str,
    profile_url: str,
) -> dict[str, Any]:
    """Parse only listing links in the profile's primary content, never recommendations."""
    canonical_profile_url = _profile_url(seller_profile_id, profile_url)
    soup = BeautifulSoup(source or "", "html.parser")
    root = _primary_root(soup)
    listing_by_id: dict[str, dict[str, Any]] = {}
    for anchor in root.find_all("a", href=True):
        href = urljoin(canonical_profile_url, str(anchor.get("href") or ""))
        parsed = urlparse(href)
        match = _DETAIL_LINK_RE.search(parsed.path)
        if parsed.scheme != "https" or parsed.hostname not in {"www.yapo.cl", "yapo.cl"} or not match:
            continue
        listing_id = match.group(1)
        card = _card_for_anchor(anchor, root)
        card_text = " ".join(unescape(card.get_text(" ", strip=True)).split())
        title = " ".join(anchor.get_text(" ", strip=True).split()) or str(anchor.get("title") or "")
        location = ""
        for node in card.find_all(True):
            attrs = " ".join((
                str(node.get("id") or ""),
                " ".join(str(value) for value in (node.get("class") or [])),
                str(node.get("data-testid") or ""),
            ))
            if re.search(r"location|comuna|district", attrs, re.I):
                location = " ".join(node.get_text(" ", strip=True).split())
                if location:
                    break
        operation, property_type = _operation_and_type(href)
        item = {
            "listing_id": listing_id,
            "url": href.split("#", 1)[0],
            "title": title,
            "comuna": location,
            "operation": operation,
            "property_type": property_type,
            "availability_status": "NO_VERIFICADO",
            "commercial_text_cues": sorted({cue.casefold() for cue in _COMMERCIAL_CUE_RE.findall(card_text)}),
        }
        listing_by_id.setdefault(listing_id, item)

    next_anchor = None
    has_pagination_controls = False
    for anchor in root.find_all("a", href=True):
        rel = " ".join(anchor.get("rel") or [])
        label = " ".join((
            str(anchor.get("aria-label") or ""),
            str(anchor.get("title") or ""),
            anchor.get_text(" ", strip=True),
        )).casefold()
        if "page" in str(anchor.get("href") or "").lower() or "pagination" in " ".join(anchor.get("class") or []).lower():
            has_pagination_controls = True
        if "next" in rel.lower() or re.search(r"\b(?:siguiente|next|p[aá]gina siguiente)\b", label, re.I):
            next_anchor = anchor
            break

    next_url = ""
    if next_anchor:
        candidate = urljoin(canonical_profile_url, str(next_anchor.get("href") or ""))
        parsed = urlparse(candidate)
        if parsed.hostname in {"www.yapo.cl", "yapo.cl"} and _PROFILE_PATH_RE.fullmatch(parsed.path):
            if _PROFILE_PATH_RE.fullmatch(parsed.path).group(1) == str(seller_profile_id):
                next_url = candidate
    explicit_empty = bool(re.search(
        r"\b(?:no hay|no tiene|sin)\s+(?:mas\s+)?(?:anuncios|publicaciones|avisos)\b",
        " ".join(root.get_text(" ", strip=True).split()),
        re.I,
    ))
    pagination_end_signal = bool(not next_url and (has_pagination_controls or explicit_empty))
    return {
        "seller_profile_id": str(seller_profile_id),
        "seller_profile_url": canonical_profile_url,
        "listings": list(listing_by_id.values()),
        "observed_listing_count": len(listing_by_id),
        "observed_listing_ids": list(listing_by_id),
        "observed_communes": sorted({item["comuna"] for item in listing_by_id.values() if item["comuna"]}),
        "observed_operations": sorted({item["operation"] for item in listing_by_id.values()}),
        "observed_property_types": sorted({item["property_type"] for item in listing_by_id.values()}),
        "commercial_text_cues": sorted({
            cue for item in listing_by_id.values() for cue in item["commercial_text_cues"]
        }),
        "next_page_url": next_url,
        "has_pagination_controls": has_pagination_controls,
        "pagination_end_signal": pagination_end_signal,
        "profile_enumeration_complete": pagination_end_signal,
        "enumeration_scope": "exact_seller_profile_page",
    }


def fetch_publisher_profile_history(
    seller_profile_id: str,
    seller_profile_url: str,
    config: Any,
    *,
    batch_id: str,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Fetch a profile through the explicit FlashProxy endpoint; fail closed if absent."""
    profile_id = str(seller_profile_id or "").strip()
    if not profile_id:
        return {"status": "NO_VERIFICADO", "reason": "missing_seller_profile_id", "observed_listing_count": 0}
    try:
        page_url = _profile_url(profile_id, seller_profile_url)
    except ValueError as exc:
        return {"status": "NO_VERIFICADO", "reason": str(exc), "observed_listing_count": 0}

    flashproxy_url = str(getattr(config, "flashproxy_proxy_url", "") or "").strip()
    if not flashproxy_url:
        return {
            "seller_profile_id": profile_id,
            "seller_profile_url": page_url,
            "status": "NO_VERIFICADO",
            "reason": "flashproxy_not_configured",
            "observed_listing_count": 0,
            "observed_listing_ids": [],
            "profile_enumeration_complete": False,
            "enumeration_scope": "not_requested",
        }

    proxy_manager = ProxyManager([flashproxy_url], provider="flashproxy")
    if not proxy_manager.has_proxies():
        return {
            "seller_profile_id": profile_id,
            "seller_profile_url": page_url,
            "status": "NO_VERIFICADO",
            "reason": "invalid_flashproxy_configuration",
            "observed_listing_count": 0,
            "observed_listing_ids": [],
            "profile_enumeration_complete": False,
            "enumeration_scope": "not_requested",
        }

    page_limit = max(1, min(int(max_pages or getattr(config, "yapo_profile_max_pages", 3) or 3), 5))
    combined: dict[str, dict[str, Any]] = {}
    pages: list[str] = []
    current_url = page_url
    end_signal = False
    source = "flashproxy"
    observed_at = ""
    for page_number in range(1, page_limit + 1):
        result: DownloadResult = download_html(
            current_url,
            config,
            proxy_manager,
            batch_id=f"profile_{profile_id}_{batch_id}",
        )
        source = result.fetch_source
        if result.validation_status in {"INVALID", "BLOCKED"}:
            return {
                "seller_profile_id": profile_id,
                "seller_profile_url": page_url,
                "status": "NO_VERIFICADO",
                "reason": f"profile_page_{result.validation_status.lower()}",
                "observed_listing_count": len(combined),
                "observed_listing_ids": list(combined),
                "profile_enumeration_complete": False,
                "pages_observed": pages,
                "availability_status": "NO_VERIFICADO",
            }
        parsed = parse_publisher_profile_page(result.html, seller_profile_id=profile_id, profile_url=current_url)
        pages.append(current_url)
        for item in parsed["listings"]:
            combined.setdefault(item["listing_id"], item)
        if result.fetch_source != "cache":
            observed_at = datetime.now(timezone.utc).isoformat()
        elif not observed_at and result.html_path.exists():
            observed_at = datetime.fromtimestamp(result.html_path.stat().st_mtime, timezone.utc).isoformat()
        current_url = parsed.get("next_page_url") or ""
        end_signal = bool(parsed.get("pagination_end_signal"))
        if not current_url:
            break

    page_limit_reached = bool(current_url and len(pages) >= page_limit)
    listings = list(combined.values())
    complete = end_signal and not page_limit_reached
    context: dict[str, Any] = {
        "seller_profile_id": profile_id,
        "seller_profile_url": page_url,
        "status": "OBSERVED" if listings else "NO_VERIFICADO",
        "reason": "profile_page_observed" if listings else "no_listing_links_found",
        "profile_source": source,
        "profile_observed_at": observed_at,
        "observed_listing_count": len(listings),
        "observed_listing_ids": sorted(combined),
        "observed_listings": listings,
        "observed_communes": sorted({item["comuna"] for item in listings if item["comuna"]}),
        "observed_operations": sorted({item["operation"] for item in listings}),
        "observed_property_types": sorted({item["property_type"] for item in listings}),
        "profile_commercial_text_cues": sorted({
            cue for item in listings for cue in item["commercial_text_cues"]
        }),
        "profile_active_listing_count": None,
        "profile_status_unknown_count": len(listings),
        "pages_observed": pages,
        "pages_limit": page_limit,
        "profile_enumeration_complete": complete,
        "enumeration_scope": "exact_seller_profile_page",
        "enumeration_note": "Observed count is a lower bound unless pagination_end_signal is true.",
    }
    if complete:
        context["distinct_listing_count"] = len(listings)
        context["enumeration_complete"] = True
    return context


def enrich_batch_publisher_profiles(
    records: list[dict[str, Any]],
    config: Any,
    *,
    batch_id: str,
) -> dict[str, Any]:
    """Cache one profile lookup per exact ID and attach the observed scope to each listing."""
    groups: dict[str, list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        profile_id = str(record.get("seller_profile_id") or "").strip()
        if profile_id:
            groups[profile_id].append(index)
    maximum = max(0, min(int(getattr(config, "yapo_profile_max_profiles", 12) or 0), 50))
    ordered = sorted(groups, key=lambda profile_id: (-len(groups[profile_id]), profile_id))
    cache: dict[str, dict[str, Any]] = {}
    looked_up = 0
    for profile_id in ordered[:maximum]:
        first = records[groups[profile_id][0]]
        profile_url = str(first.get("seller_profile_url") or f"/user/profile/id/{profile_id}")
        cache[profile_id] = fetch_publisher_profile_history(
            profile_id,
            profile_url,
            config,
            batch_id=batch_id,
        )
        looked_up += int(cache[profile_id].get("status") == "OBSERVED")
    for profile_id, indexes in groups.items():
        local_ids = list(dict.fromkeys(str(records[index].get("listing_id") or "") for index in indexes if records[index].get("listing_id")))
        remote = cache.get(profile_id)
        for index in indexes:
            record = records[index]
            existing = dict(record.get("publisher_profile_context") or {})
            local_context = {
                "seller_profile_id": profile_id,
                "local_batch_distinct_listing_count": len(local_ids),
                "local_batch_listing_ids": local_ids,
                "local_batch_scope": "current_search_batch",
                "profile_enumeration_complete": False,
                "profile_status": "NO_VERIFICADO",
                "profile_reason": "profile_request_limit_reached" if profile_id not in cache else "profile_not_observed",
            }
            if remote is not None:
                local_context.update(remote)
            record["publisher_profile_context"] = {**existing, **local_context}
            if isinstance(record.get("details"), dict):
                record["details"]["publisher_profile_context"] = dict(record["publisher_profile_context"])
    return {
        "distinct_seller_profile_ids": len(groups),
        "profile_requests_attempted": len(cache),
        "profile_pages_observed": looked_up,
        "profile_limit": maximum,
        "profiles_with_complete_enumeration": sum(
            bool(context.get("profile_enumeration_complete")) for context in cache.values()
        ),
        "flashproxy_configured": bool(str(getattr(config, "flashproxy_proxy_url", "") or "").strip()),
    }
