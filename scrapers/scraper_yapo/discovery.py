from __future__ import annotations

import json
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


def _discovery_commune_evidence(value: str, target_communes: set[str]) -> str:
    """A listing-card location is advisory; compound or missing values stay pending."""
    value = (value or "").strip()
    if not value:
        return "pending_missing"
    normalized = _norm_commune(value)
    for target in target_communes:
        if normalized == target:
            return "possible_match"
        if normalized.startswith(target + "-"):
            return "pending_compound"
    if "," in value or "/" in value or "|" in value:
        return "pending_compound"
    return "clear_other_commune"


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


_PAGINATION_URL_ATTRIBUTES = (
    "href",
    "data-href",
    "data-url",
    "data-next",
    "data-next-url",
    "data-next-href",
    "data-page-url",
    "data-pagination-url",
)
_NEXT_KEY_NAMES = {
    "next",
    "nexthref",
    "nextlink",
    "nextpageurl",
    "nexturl",
    "nextpagehref",
    "nextpagelink",
    "nextpaginationurl",
}
_CURRENT_PAGE_KEY_NAMES = {"currentpage", "currentpagenumber", "pagecurrent"}
_TOTAL_PAGE_KEY_NAMES = {"pagecount", "pagecounttotal", "totalpages", "totalpagecount"}
_NEXT_LABEL_RE = re.compile(r"(?:^|[\s_-])(next|siguiente|proxim(?:a|o)|p(?:á|a)gina\s+siguiente)(?:$|[\s_-])", re.I)


def _compact_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _looks_like_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate or candidate.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    parsed = urlparse(candidate)
    return bool(parsed.scheme in {"http", "https"} or candidate.startswith(("/", "?", "./", "../")))


def _explicit_pagination_url(node: Any, page_url: str) -> str | None:
    for attribute in _PAGINATION_URL_ATTRIBUTES:
        value = node.get(attribute)
        if _looks_like_url(value):
            return _normalize_url(urljoin(page_url, str(value).strip()))
    return None


def _node_is_disabled(node: Any) -> bool:
    aria_disabled = str(node.get("aria-disabled") or "").strip().lower()
    if aria_disabled == "true" or node.has_attr("disabled"):
        return True
    classes = " ".join(node.get("class") or []).lower()
    return bool(re.search(r"(?:^|[\s_-])disabled(?:$|[\s_-])", classes))


def _node_label(node: Any) -> str:
    values = [
        node.get_text(" ", strip=True),
        str(node.get("aria-label") or ""),
        str(node.get("title") or ""),
        str(node.get("data-testid") or ""),
        " ".join(node.get("class") or []),
        str(node.get("id") or ""),
    ]
    return " ".join(value for value in values if value).strip().lower()


def _pagination_containers(soup: BeautifulSoup) -> list[Any]:
    return soup.select(
        'nav[aria-label*="pagin" i], nav[aria-label*="page" i], '
        '[class*="pagination"], [class*="pager"], [id*="pagination"], '
        '[id*="pager"], [data-pagination], [role="navigation"]'
    )


def _embedded_pagination_values(html: str) -> tuple[list[str], int | None, int | None]:
    """Read pagination URLs/metadata that the page embeds in JSON.

    Only values explicitly present under pagination-like keys are accepted.
    Numeric page values are metadata only; they are never converted into a URL.
    """
    next_urls: list[str] = []
    current_page: int | None = None
    total_pages: int | None = None
    soup = BeautifulSoup(html, "html.parser")

    def walk(value: Any, key: str = "") -> None:
        nonlocal current_page, total_pages
        compact = _compact_key(key)
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                walk(child_value, str(child_key))
            return
        if isinstance(value, list):
            for child_value in value:
                walk(child_value, key)
            return
        if compact in _NEXT_KEY_NAMES and _looks_like_url(value):
            next_urls.append(str(value).strip())
        elif compact in _CURRENT_PAGE_KEY_NAMES and isinstance(value, int) and not isinstance(value, bool):
            current_page = value
        elif compact in _TOTAL_PAGE_KEY_NAMES and isinstance(value, int) and not isinstance(value, bool):
            total_pages = value

    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        candidate = raw.strip()
        if not candidate.startswith(("{", "[")):
            continue
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        walk(payload)
    return next_urls, current_page, total_pages


def _pagination_metadata(soup: BeautifulSoup, html: str) -> tuple[int | None, int | None, bool]:
    containers = _pagination_containers(soup)
    current_page: int | None = None
    total_pages: int | None = None
    disabled_next = False

    for container in containers:
        for node in container.find_all(True):
            for attribute in ("data-current-page", "data-page-current", "aria-current"):
                value = str(node.get(attribute) or "")
                match = re.fullmatch(r"\s*(\d+)\s*", value)
                if match:
                    current_page = int(match.group(1))
            for attribute in ("data-total-pages", "data-page-count", "data-total", "aria-setsize"):
                value = str(node.get(attribute) or "")
                match = re.fullmatch(r"\s*(\d+)\s*", value)
                if match:
                    total_pages = int(match.group(1))
            text = node.get_text(" ", strip=True)
            if current_page is None and node.get("aria-current") == "page":
                match = re.search(r"\b(\d+)\b", text)
                if match:
                    current_page = int(match.group(1))
            if _NEXT_LABEL_RE.search(_node_label(node)) and _node_is_disabled(node):
                disabled_next = True

        if current_page is None:
            current = container.select_one('[aria-current="page"], .active, .current')
            if current:
                match = re.search(r"\b(\d+)\b", current.get_text(" ", strip=True))
                if match:
                    current_page = int(match.group(1))

    _, embedded_current, embedded_total = _embedded_pagination_values(html)
    if current_page is None:
        current_page = embedded_current
    if total_pages is None:
        total_pages = embedded_total
    return current_page, total_pages, disabled_next


def _pagination_state(html: str, page_url: str) -> tuple[str | None, bool, str]:
    """Read only explicit pagination URLs and terminal state from Yapo HTML.

    This function deliberately never manufactures ``?page=2``/``?o=2`` URLs.
    If the document exposes only a numeric page count without a next URL, the
    caller must stop as incomplete because advancing would be an assumption.
    """
    soup = BeautifulSoup(html, "html.parser")

    next_nodes = soup.select('link[rel~="next"], a[rel~="next"]')
    for node in next_nodes:
        if not _node_is_disabled(node):
            next_url = _explicit_pagination_url(node, page_url)
            if next_url:
                return next_url, False, "next_link"

    containers = _pagination_containers(soup)
    for container in containers:
        for node in container.find_all(["a", "button"]):
            if _node_is_disabled(node):
                continue
            label = _node_label(node)
            is_next_label = bool(_NEXT_LABEL_RE.search(label) or label.strip() in {"›", "»", ">"})
            if is_next_label:
                next_url = _explicit_pagination_url(node, page_url)
                if next_url:
                    return next_url, False, "next_control"

    # Some server-rendered variants omit a pagination wrapper but still expose
    # a semantic next link. Arrow-only controls are intentionally restricted to
    # the pagination containers above to avoid mistaking unrelated ``>`` links.
    for node in soup.find_all(["a", "button"]):
        if _node_is_disabled(node):
            continue
        label = _node_label(node)
        if _NEXT_LABEL_RE.search(label):
            next_url = _explicit_pagination_url(node, page_url)
            if next_url:
                return next_url, False, "next_label"

    # A framework may expose the next URL through a data-* attribute on a
    # non-link element. The attribute itself must contain the URL; a numeric
    # ``data-next-page=2`` is not enough to synthesize a request.
    for node in soup.find_all(True):
        if any(node.has_attr(attribute) for attribute in _PAGINATION_URL_ATTRIBUTES[1:]):
            label = _node_label(node)
            if _NEXT_LABEL_RE.search(label) or any(
                str(attribute).lower().startswith("data-next")
                for attribute in node.attrs
            ):
                next_url = _explicit_pagination_url(node, page_url)
                if next_url:
                    return next_url, False, "next_data_attribute"

    embedded_next_urls, _, _ = _embedded_pagination_values(html)
    if embedded_next_urls:
        return _normalize_url(urljoin(page_url, embedded_next_urls[0])), False, "embedded_next_url"

    current_page, total_pages, disabled_next = _pagination_metadata(soup, html)
    for container in containers:
        last_links = container.select(
            'a[rel~="last"][href], a[aria-label*="last" i][href], '
            'a[title*="last" i][href], a[class*=last][href]'
        )
        if current_page is not None:
            for node in last_links:
                last_url = _explicit_pagination_url(node, page_url)
                if last_url == _normalize_url(page_url):
                    return None, True, "explicit_last_page_link_matches_current"
    if current_page is not None and total_pages is not None and current_page == total_pages:
        return None, True, "explicit_total_pages_reached"
    if disabled_next:
        return None, True, "explicit_disabled_next_control"
    if containers:
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
                report["pages"].append({
                    "page_number": page_number,
                    "url": current_url,
                    "status": "blocked" if validation["status"] == "BLOCKED" else "invalid",
                    "http_status": download.status_code,
                    "listing_count": None,
                    "duplicate_count": None,
                    "validation": validation["status"],
                    "validation_reason": validation["reason"],
                })
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
                discovery_record = dict(item)
                if target_norm:
                    discovery_record["discovery_commune_evidence"] = _discovery_commune_evidence(
                        item.get("discovery_comuna", ""), target_norm
                    )
                # Do not discard on card location. The detail page is the source
                # used by the pilot to confirm the commune or leave it pending.
                discovered.append({**discovery_record, "url": url, "source_search_url": start_url, "source_page_url": current_url, "page_number": page_number, "discovered_at": _utcnow(), "batch_id": batch_id})
                page_new += 1
                if max_urls is not None and len(discovered) >= max_urls:
                    report["pages"].append({"page_number": page_number, "url": current_url, "status": "complete", "listing_count": len(page_records), "duplicate_count": duplicate_count, "new_count": page_new, "anchors_total": anchors_total})
                    report["pagination_complete"] = False
                    report["pagination_cause"] = "safety_max_urls_reached"
                    return discovered
            report["pages"].append({"page_number": page_number, "url": current_url, "status": "complete" if page_records else "empty", "listing_count": len(page_records), "duplicate_count": duplicate_count, "new_count": page_new, "anchors_total": anchors_total})
            next_url, terminal_verified, evidence = _pagination_state(download.html, current_url)
            report["pages"][-1]["pagination_evidence"] = evidence
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
