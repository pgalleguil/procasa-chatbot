from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from bs4 import BeautifulSoup

from classifier_rules import load_rule_sets
from config import AppConfig
from downloader import download_html, validate_html
from proxy_manager import ProxyManager


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return urlunparse(parsed._replace(fragment=""))


def _sanitize_query(text: str) -> str:
    return re.sub(r"\s+", "+", text.strip().lower())


def load_default_search_terms() -> list[str]:
    terms = load_rule_sets().get("search_terms", [])
    return [str(item) for item in terms if str(item).strip()]


def build_default_start_urls(config: AppConfig) -> list[str]:
    template = config.yapo_search_url_template
    return [template.format(query=_sanitize_query(term)) for term in load_default_search_terms()]


def build_next_page_url(current_url: str, page_number: int) -> str:
    parsed = urlparse(current_url)
    query_items = list(parse_qsl(parsed.query, keep_blank_values=True))
    preferred_keys = {"pagina", "page", "o"}
    updated_items: list[tuple[str, str]] = []
    matched = False

    for key, value in query_items:
        if key in preferred_keys:
            updated_items.append((key, str(page_number)))
            matched = True
        else:
            updated_items.append((key, value))

    if not matched:
        path = parsed.path.rstrip("/")
        if re.search(r"/(page|pagina|o)/\d+$", path):
            path = re.sub(r"/(page|pagina|o)/\d+$", rf"/\1/{page_number}", path)
        else:
            updated_items.append(("page", str(page_number)))
        return urlunparse(parsed._replace(path=path, query=urlencode(updated_items)))

    return urlunparse(parsed._replace(query=urlencode(updated_items)))


def is_listing_detail_url(url: str) -> bool:
    low = url.lower()
    if not any(prefix in low for prefix in (
        "/bienes-raices-venta-de-propiedades", "/bienes-raices-alquiler-",
    )):
        return False
    if "/searchresult/" in low:
        return False
    
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not re.search(r"/\d{6,}$", path):
        return False
        
    return True


def normalize_yapo_url(href: str, base_url: str = "https://www.yapo.cl") -> str:
    full = urljoin(base_url, href.strip())
    parsed = urlparse(full)
    
    parsed = parsed._replace(fragment="")
    
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    clean_query = [(k, v) for k, v in query_items if not k.lower().startswith("utm_") and k.lower() not in {"clid", "gclid", "fbclid"}]
    parsed = parsed._replace(query=urlencode(clean_query))
    
    return urlunparse(parsed)


def _norm_commune(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _extract_listing_urls(html: str, page_url: str) -> tuple[list[dict[str, str]], int]:
    anchors = re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, flags=re.I)
    anchors_total = len(anchors)
    candidates: list[dict[str, str]] = []
    soup = BeautifulSoup(html, "html.parser")
    for tile in soup.select(".d3-ad-tile"):
        full_url = ""
        for anchor in tile.find_all("a", href=True):
            candidate = normalize_yapo_url(str(anchor.get("href") or ""), page_url)
            if is_listing_detail_url(candidate):
                full_url = candidate
                break
        if not full_url:
            continue
        location = tile.select_one(".d3-ad-tile__location")
        title = tile.select_one(".d3-ad-tile__title")
        seller = tile.select_one(".d3-ad-tile__seller")
        candidates.append({
            "url": full_url,
            "discovery_comuna": location.get_text(" ", strip=True) if location else "",
            "discovery_title": title.get_text(" ", strip=True) if title else "",
            "discovery_seller": seller.get_text(" ", strip=True) if seller else "",
        })
    if not candidates:
        for href in anchors:
            full_url = normalize_yapo_url(href, page_url)
            if is_listing_detail_url(full_url):
                candidates.append({"url": full_url, "discovery_comuna": "", "discovery_title": "", "discovery_seller": ""})
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for item in candidates:
        url = item["url"]
        if url not in seen:
            seen.add(url)
            deduped.append(item)
    return deduped, anchors_total


def _find_next_page_url(html: str, page_url: str, fallback_page_number: int) -> str | None:
    match = re.search(r'<a[^>]+rel=["\']next["\'][^>]*href=["\']([^"\']+)["\']', html, flags=re.I)
    if match:
        return _normalize_url(urljoin(page_url, match.group(1)))

    for pattern in (
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*(?:siguiente|next|»|>|pr[oó]xima)\s*</a>',
        r'<a[^>]*class=["\'][^"\']*next[^"\']*["\'][^>]*href=["\']([^"\']+)["\']',
    ):
        match = re.search(pattern, html, flags=re.I)
        if match:
            return _normalize_url(urljoin(page_url, match.group(1)))

    if any(token in page_url for token in ("pagina=", "page=", "o=")):
        return build_next_page_url(page_url, fallback_page_number)
    if re.search(r"/(page|pagina|o)/\d+$", page_url):
        return build_next_page_url(page_url, fallback_page_number)
    return build_next_page_url(page_url, fallback_page_number)


def discover_listing_urls(
    start_urls: list[str],
    max_pages: int | None = 10,
    max_urls: int = 1000,
    until_end: bool = False,
    batch_id: str | None = None,
    target_communes: list[str] | None = None,
) -> list[dict]:
    config = AppConfig()
    config.ensure_layout()
    proxy_manager = ProxyManager.from_config(config)
    batch_id = batch_id or config.generate_batch_id()
    discovered: list[dict] = []
    seen_urls: set[str] = set()
    target_norm = {_norm_commune(value) for value in (target_communes or []) if value}

    if not start_urls:
        start_urls = build_default_start_urls(config)

    for start_url in start_urls:
        current_url = _normalize_url(start_url)
        page_number = 1
        pages_visited = 0

        while True:
            if max_pages is not None and pages_visited >= max_pages:
                break

            try:
                download = download_html(current_url, config, proxy_manager, batch_id=batch_id)
            except Exception as e:
                print(f"Download failed for {current_url}: {e}")
                break

            validation = validate_html(download.html)
            if validation["status"] in {"INVALID", "BLOCKED", "LISTING_REMOVED"}:
                print(f"Validation failed for {current_url}: {validation['status']}")
                break

            page_records, anchors_total = _extract_listing_urls(download.html, current_url)
            listing_links = len(page_records)
            new_on_page = 0
            raw_new_on_page = 0
            for item in page_records:
                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                raw_new_on_page += 1
                if target_norm and _norm_commune(item.get("discovery_comuna", "")) not in target_norm:
                    continue
                discovered.append(
                    {
                        **item,
                        "url": url,
                        "source_search_url": start_url,
                        "source_page_url": current_url,
                        "page_number": page_number,
                        "discovered_at": _utcnow(),
                        "batch_id": batch_id,
                    }
                )
                new_on_page += 1
                if len(discovered) >= max_urls:
                    print(f"Discovery page {page_number}: anchors_total={anchors_total}, listing_links={listing_links}, new_links={new_on_page}, page_url={current_url}")
                    return discovered

            print(f"Discovery page {page_number}: anchors_total={anchors_total}, listing_links={listing_links}, new_links={new_on_page}, page_url={current_url}")

            if raw_new_on_page == 0:
                break

            pages_visited += 1
            page_number += 1
            next_page_url = _find_next_page_url(download.html, current_url, page_number)
            if not next_page_url or next_page_url == current_url:
                break
            current_url = next_page_url

            if not until_end and max_pages is None:
                break

    return discovered


if __name__ == "__main__":
    assert is_listing_detail_url("https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/atencion-familias-e-inversionistas-oportunidad-unica-en-la-florida/32588092")
    assert is_listing_detail_url("/bienes-raices-venta-de-propiedades-apartamentos/departamento-en-venta/32464287")
    assert not is_listing_detail_url("https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana")
    print("All tests passed.")
