from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

try:
    from .classifier_rules import load_rule_sets
    from .config import AppConfig
    from .downloader import download_html, html_path_for_url, validate_html
    from .proxy_manager import ProxyManager
except ImportError:  # direct script execution
    from classifier_rules import load_rule_sets
    from config import AppConfig
    from downloader import download_html, html_path_for_url, validate_html
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


def is_listing_detail_url(url: str) -> bool:
    low = url.lower()
    if not any(prefix in low for prefix in (
        "/bienes-raices-venta-de-propiedades", "/bienes-raices-alquiler-",
    )):
        return False
    if "/searchresult/" in low:
        return False
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname not in {"www.yapo.cl", "yapo.cl"}:
        return False
    return bool(re.search(r"/\d{6,}$", parsed.path.rstrip("/")))


def normalize_yapo_url(href: str, base_url: str = "https://www.yapo.cl") -> str:
    full = urljoin(base_url, href.strip())
    parsed = urlparse(full)._replace(fragment="")
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    clean_query = [(k, v) for k, v in query_items if not k.lower().startswith("utm_") and k.lower() not in {"clid", "gclid", "fbclid"}]
    return urlunparse(parsed._replace(query=urlencode(clean_query)))


def _norm_commune(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _extract_listing_urls_detailed(html: str, page_url: str) -> tuple[list[dict[str, str]], int, int]:
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)
    candidates: list[dict[str, str]] = []
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
        for anchor in anchors:
            full_url = normalize_yapo_url(str(anchor.get("href") or ""), page_url)
            if is_listing_detail_url(full_url):
                candidates.append({"url": full_url, "discovery_comuna": "", "discovery_title": "", "discovery_seller": ""})
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    duplicates = 0
    for item in candidates:
        if item["url"] in seen:
            duplicates += 1
        else:
            seen.add(item["url"])
            deduped.append(item)
    return deduped, len(anchors), duplicates


def _extract_listing_urls(html: str, page_url: str) -> tuple[list[dict[str, str]], int]:
    records, anchor_count, _ = _extract_listing_urls_detailed(html, page_url)
    return records, anchor_count


def _pagination_state(html: str, page_url: str) -> tuple[str | None, bool, str]:
    """Read only URLs and terminal state actually present in Yapo's HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select('link[rel~="next"], a[rel~="next"]'):
        if node.get("href"):
            return _normalize_url(urljoin(page_url, str(node["href"]))), False, "next_link"
    for node in soup.find_all("a", href=True):
        label = " ".join((node.get_text(" ", strip=True), str(node.get("aria-label") or ""), str(node.get("title") or ""))).strip().lower()
        if label in {"siguiente", "next", "próxima", "proxima", "siguiente página", "next page", "›", "»", ">"}:
            return _normalize_url(urljoin(page_url, str(node["href"]))), False, "next_label"
    pager_nodes = soup.select("[class*=pagination], [class*=pager], nav[aria-label*=pagin], nav[aria-label*=Pagination]")
    if pager_nodes:
        pager = BeautifulSoup(" ".join(str(node) for node in pager_nodes), "html.parser")
        current = pager.select_one('[aria-current="page"], .active, .current')
        current_match = re.search(r"\d+", current.get_text(" ", strip=True)) if current else None
        total_pages = pager.select_one("[data-total-pages], [data-page-count]")
        if current_match and total_pages:
            total = total_pages.get("data-total-pages") or total_pages.get("data-page-count")
            if str(total).isdigit() and int(current_match.group()) == int(total):
                return None, True, "explicit_total_pages_reached"
        last_links = pager.select('a[rel~="last"][href], a[aria-label*="last" i][href], a[title*="last" i][href], a[class*=last][href]')
        if current_match and last_links:
            last_url = _normalize_url(urljoin(page_url, str(last_links[-1]["href"])))
            if last_url == _normalize_url(page_url):
                return None, True, "explicit_last_page_link_matches_current"
        disabled_next = pager.select('[rel~="next"][aria-disabled="true"], [rel~="next"].disabled, .next.disabled, .pagination-next[aria-disabled="true"]')
        if current_match and disabled_next:
            return None, True, "explicit_disabled_next_control"
        return None, False, "pagination_end_not_explicitly_verified"
    text = soup.get_text(" ", strip=True).lower()
    empty_markers = ("no se encontraron resultados", "no hay resultados", "sin resultados", "no results found")
    has_listings = bool(_extract_listing_urls(html, page_url)[0])
    if not has_listings and any(marker in text for marker in empty_markers):
        return None, True, "explicit_empty_results"
    return None, False, "pagination_metadata_missing"


def discover_listing_urls(
    start_urls: list[str],
    max_pages: int | None = None,
    max_urls: int | None = None,
    until_end: bool = True,
    batch_id: str | None = None,
    target_communes: list[str] | None = None,
    report: dict[str, Any] | None = None,
    config: AppConfig | None = None,
    proxy_manager: ProxyManager | None = None,
) -> list[dict]:
    config = config or AppConfig()
    config.ensure_layout()
    proxy_manager = proxy_manager or ProxyManager.from_config(config)
    batch_id = batch_id or config.generate_batch_id()
    discovered: list[dict] = []
    seen_urls: set[str] = set()
    target_norm = {_norm_commune(value) for value in (target_communes or []) if value}
    report = report if report is not None else {}
    report.update({"pages": [], "pagination_complete": True, "pagination_cause": "", "errors": [], "proxy_requests": 0, "request_attempts": 0, "successful_downloads": 0, "cache_responses": 0})
    if not start_urls:
        start_urls = build_default_start_urls(config)

    for start_url in start_urls:
        current_url = _normalize_url(start_url)
        if urlparse(current_url).hostname not in {"www.yapo.cl", "yapo.cl"}:
            report["pagination_complete"] = False
            report["pagination_cause"] = "unauthorized_start_domain"
            report["errors"].append("unauthorized Yapo start domain")
            break
        page_number = 1
        visited_page_urls: set[str] = set()
        while True:
            if current_url in visited_page_urls:
                report["pagination_complete"] = False
                report["pagination_cause"] = "pagination_url_cycle"
                break
            visited_page_urls.add(current_url)
            if max_pages is not None and page_number > max_pages:
                report["pagination_complete"] = False
                report["pagination_cause"] = "safety_max_pages_reached"
                break
            try:
                cache_exists = html_path_for_url(current_url, config, batch_id=batch_id).exists()
                if not cache_exists and proxy_manager.has_proxies():
                    report["request_attempts"] += 1
                download = download_html(current_url, config, proxy_manager, batch_id=batch_id)
            except Exception as exc:
                message = str(exc)
                report["pages"].append({"page_number": page_number, "url": current_url, "status": "blocked" if "BLOCKED" in message else "error", "listing_count": None, "duplicate_count": None, "error": message})
                report["errors"].append(message)
                report["pagination_complete"] = False
                report["pagination_cause"] = "blocked_or_download_error"
                break
            if download.fetch_source == "cache":
                report["cache_responses"] += 1
            elif download.fetch_source == "requests_proxy":
                report["successful_downloads"] += 1
                report["proxy_requests"] += 1
            validation = validate_html(download.html)
            if validation["status"] in {"INVALID", "BLOCKED", "LISTING_REMOVED"}:
                report["pages"].append({"page_number": page_number, "url": current_url, "status": "blocked" if validation["status"] == "BLOCKED" else "invalid", "listing_count": None, "duplicate_count": None, "validation": validation["status"]})
                report["pagination_complete"] = False
                report["pagination_cause"] = f"html_{validation['status'].lower()}"
                break
            page_records, anchors_total, page_duplicates = _extract_listing_urls_detailed(download.html, current_url)
            page_new = 0
            duplicate_count = page_duplicates
            for item in page_records:
                url = item["url"]
                if url in seen_urls:
                    duplicate_count += 1
                    continue
                seen_urls.add(url)
                if target_norm and _norm_commune(item.get("discovery_comuna", "")) not in target_norm:
                    continue
                discovered.append({**item, "url": url, "source_search_url": start_url, "source_page_url": current_url, "page_number": page_number, "discovered_at": _utcnow(), "batch_id": batch_id})
                page_new += 1
                if max_urls is not None and len(discovered) >= max_urls:
                    report["pages"].append({"page_number": page_number, "url": current_url, "status": "complete", "listing_count": len(page_records), "duplicate_count": duplicate_count, "new_count": page_new, "anchors_total": anchors_total})
                    report["pagination_complete"] = False
                    report["pagination_cause"] = "safety_max_urls_reached"
                    return discovered
            report["pages"].append({"page_number": page_number, "url": current_url, "status": "complete" if page_records else "empty", "listing_count": len(page_records), "duplicate_count": duplicate_count, "new_count": page_new, "anchors_total": anchors_total})
            next_url, terminal_verified, evidence = _pagination_state(download.html, current_url)
            if next_url:
                if urlparse(next_url).hostname not in {"www.yapo.cl", "yapo.cl"}:
                    report["pagination_complete"] = False
                    report["pagination_cause"] = "unauthorized_pagination_domain"
                    report["errors"].append("pagination link outside authorized Yapo domains")
                    break
                if next_url in visited_page_urls:
                    report["pagination_complete"] = False
                    report["pagination_cause"] = "pagination_url_cycle"
                    break
                current_url = next_url
                page_number += 1
                continue
            if terminal_verified:
                report["pagination_cause"] = evidence
                break
            report["pagination_complete"] = False
            report["pagination_cause"] = evidence
            break
    if not report["pagination_cause"]:
        report["pagination_cause"] = "verified_end"
    return discovered


if __name__ == "__main__":
    assert is_listing_detail_url("https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/atencion-familias/32588092")
    assert is_listing_detail_url("/bienes-raices-venta-de-propiedades-apartamentos/departamento-en-venta/32464287")
    assert not is_listing_detail_url("https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana")
    print("All tests passed.")
