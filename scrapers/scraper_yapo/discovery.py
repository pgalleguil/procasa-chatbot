from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

try:
    from .classifier_rules import load_rule_sets
    from .config import AppConfig
    from .downloader import DownloadResult, download_html, html_path_for_url, validate_html
    from .proxy_manager import ProxyManager
except ImportError:  # direct script execution
    from classifier_rules import load_rule_sets
    from config import AppConfig
    from downloader import DownloadResult, download_html, html_path_for_url, validate_html
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


def _fold_pagination_label(value: str) -> str:
    return unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()


def _node_is_next_control(node: Any, *, allow_arrows: bool = False) -> bool:
    text = _fold_pagination_label(node.get_text(" ", strip=True))
    attributes = " ".join(
        str(node.get(attribute) or "")
        for attribute in ("aria-label", "title", "data-testid", "class", "id")
    )
    folded_attributes = _fold_pagination_label(attributes)
    if re.search(r"\b(?:next|previous)\s+slide\b", text) or re.search(
        r"\b(?:next|previous)\s+slide\b", folded_attributes
    ):
        return False
    exact_labels = {"next", "next page", "siguiente", "pagina siguiente", "proxima", "proximo"}
    if text in exact_labels:
        return True
    if allow_arrows and text in {"›", "»", ">"}:
        return True
    return bool(_NEXT_LABEL_RE.search(folded_attributes))


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

    candidates: list[str] = []
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        candidate = raw.strip()
        candidates.append(candidate)
        for encoded in re.findall(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)', candidate, re.S):
            try:
                candidates.append(str(json.loads(encoded)))
            except (TypeError, ValueError):
                continue

    for candidate in candidates:
        if candidate.startswith(("{", "[")):
            try:
                walk(json.loads(candidate))
            except (TypeError, ValueError):
                pass
        for metadata in re.findall(r'"metadata"\s*:\s*\{([^{}]*)\}', candidate, re.I):
            current_match = re.search(r'"page"\s*:\s*(\d+)', metadata, re.I)
            total_match = re.search(r'"totalPages"\s*:\s*(\d+)', metadata, re.I)
            if current_match:
                current_page = int(current_match.group(1))
            if total_match:
                total_pages = int(total_match.group(1))
        for next_value in re.findall(
            r'"(?:nextUrl|next_url|nextHref|nextLink)"\s*:\s*"([^"\\]+)', candidate, re.I
        ):
            if _looks_like_url(next_value):
                next_urls.append(next_value)
    return next_urls, current_page, total_pages


def _pagination_metadata(soup: BeautifulSoup, html: str) -> tuple[int | None, int | None, bool]:
    containers = _pagination_containers(soup)
    current_page: int | None = None
    total_pages: int | None = None
    disabled_next = False

    for container in containers:
        for node in [container, *container.find_all(True)]:
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
            if _node_is_next_control(node, allow_arrows=True) and _node_is_disabled(node):
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
    next_control_without_url = False
    for container in containers:
        for node in container.find_all(["a", "button"]):
            if _node_is_disabled(node):
                continue
            if _node_is_next_control(node, allow_arrows=True):
                next_control_without_url = True
                next_url = _explicit_pagination_url(node, page_url)
                if next_url:
                    return next_url, False, "next_control"

    # Some server-rendered variants omit a pagination wrapper but still expose
    # a semantic next link. Arrow-only controls are intentionally restricted to
    # the pagination containers above to avoid mistaking unrelated ``>`` links.
    for node in soup.find_all(["a", "button"]):
        if _node_is_disabled(node):
            continue
        if _node_is_next_control(node):
            next_control_without_url = True
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
    if next_control_without_url:
        return None, False, "pagination_control_without_explicit_url"
    if containers:
        return None, False, "pagination_end_not_explicitly_verified"
    text = soup.get_text(" ", strip=True).lower()
    empty_markers = ("no se encontraron resultados", "no hay resultados", "sin resultados", "no results found")
    has_listings = bool(_extract_listing_urls(html, page_url)[0])
    if not has_listings and any(marker in text for marker in empty_markers):
        return None, True, "explicit_empty_results"
    return None, False, "pagination_metadata_missing"


_YAPO_PAGINATION_NAV_SELECTOR = 'nav[aria-label*="pagin" i]'
_YAPO_NEXT_BUTTON_SELECTOR = (
    f'{_YAPO_PAGINATION_NAV_SELECTOR} button[aria-label*="siguiente" i], '
    f'{_YAPO_PAGINATION_NAV_SELECTOR} button[aria-label="next" i], '
    f'{_YAPO_PAGINATION_NAV_SELECTOR} button[aria-label*="next page" i]'
)


def _has_client_side_pagination(html: str) -> bool:
    """Return true only for an enabled next button inside Yapo's pager nav."""
    soup = BeautifulSoup(html, "html.parser")
    for nav in soup.select(_YAPO_PAGINATION_NAV_SELECTOR):
        for button in nav.find_all("button"):
            if _node_is_disabled(button):
                continue
            if _node_is_next_control(button):
                return True
    return False


def _playwright_proxy_config(proxy_url: str) -> dict[str, str]:
    parsed = urlparse(proxy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("YAPO_PROXY_INVALID: FlashProxy no tiene un endpoint compatible.")
    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
    result = {"server": server}
    if parsed.username is not None:
        result["username"] = unquote(parsed.username)
    if parsed.password is not None:
        result["password"] = unquote(parsed.password)
    return result


class _YapoClientPagination:
    """Drive Yapo's real React pager without constructing an undocumented URL."""

    def __init__(
        self,
        page_url: str,
        page_html: str,
        config: AppConfig,
        proxy_manager: ProxyManager,
    ) -> None:
        proxy = proxy_manager.next_proxy() if proxy_manager.has_proxies() else None
        if proxy is None:
            raise RuntimeError("YAPO_PROXY_REQUIRED: la paginación JavaScript exige FlashProxy.")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on Windows runtime setup
            raise RuntimeError("YAPO_PLAYWRIGHT_REQUIRED: instale las dependencias del scraper.") from exc

        self._start_url = _normalize_url(page_url)
        self._initial_html = page_html
        self._config = config
        self._request_events: list[dict[str, str]] = []
        self._initial_html_served = False
        self._timeout_ms = max(10, int(config.request_timeout_seconds)) * 1000
        self._playwright = sync_playwright().start()
        proxy_config = _playwright_proxy_config(proxy.url)
        try:
            self._browser = self._playwright.chromium.launch(
                headless=True,
                channel="chrome",
                proxy=proxy_config,
            )
        except Exception as exc:  # pragma: no cover - depends on installed Windows Chrome
            self._playwright.stop()
            raise RuntimeError(f"YAPO_PLAYWRIGHT_LAUNCH_FAILED: {type(exc).__name__}.") from None

        self._context = self._browser.new_context(
            user_agent=config.user_agent,
            locale="es-CL",
            java_script_enabled=True,
        )
        self._page = self._context.new_page()
        self._page.on("request", self._record_request)
        self._page.route("**/*", self._route_request)
        try:
            self._page.goto(
                self._start_url,
                wait_until="domcontentloaded",
                timeout=self._timeout_ms,
            )
            self._page.locator(_YAPO_PAGINATION_NAV_SELECTOR).first.wait_for(
                state="attached",
                timeout=self._timeout_ms,
            )
            self._wait_for_network_quiet()
            if self._current_page_number() != 1:
                raise RuntimeError("YAPO_PAGINATION_INITIAL_PAGE_MISMATCH")
        except Exception as exc:
            self.close()
            if isinstance(exc, RuntimeError) and str(exc).startswith("YAPO_"):
                raise
            raise RuntimeError(f"YAPO_PAGINATION_BROWSER_INIT_FAILED: {type(exc).__name__}.") from None

    def _route_request(self, route: Any) -> None:
        request = route.request
        parsed = urlparse(request.url)
        host = (parsed.hostname or "").lower()
        if host != "yapo.cl" and not host.endswith(".yapo.cl"):
            route.abort()
            return
        if request.resource_type in {"image", "media", "font"}:
            route.abort()
            return
        if (
            not self._initial_html_served
            and request.is_navigation_request()
            and _normalize_url(request.url) == self._start_url
        ):
            self._initial_html_served = True
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=self._initial_html,
            )
            return
        route.continue_()

    def _record_request(self, request: Any) -> None:
        parsed = urlparse(request.url)
        host = (parsed.hostname or "").lower()
        if host == "yapo.cl" or host.endswith(".yapo.cl"):
            # Store the request that the page itself made; proxy credentials are
            # not part of browser request URLs and are never logged here.
            self._request_events.append({
                "method": request.method,
                "url": _normalize_url(request.url),
                "resource_type": request.resource_type,
            })

    def _current_page_number(self) -> int | None:
        current = self._page.locator(
            f'{_YAPO_PAGINATION_NAV_SELECTOR} [aria-current="page"]'
        ).first
        if current.count() == 0:
            return None
        label = current.get_attribute("aria-label") or current.inner_text()
        match = re.search(r"\d+", label or "")
        return int(match.group(0)) if match else None

    def _wait_for_network_quiet(self) -> None:
        try:
            self._page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            # Yapo may keep analytics open; page-number and listing checks below
            # are the completion signal for each transition.
            pass

    def advance(self, expected_page: int) -> dict[str, Any]:
        button = self._page.locator(_YAPO_NEXT_BUTTON_SELECTOR).first
        if button.count() == 0:
            return {"terminal": False, "evidence": "client_next_button_missing"}
        if button.is_disabled() or (button.get_attribute("aria-disabled") or "").lower() == "true":
            return {"terminal": True, "evidence": "explicit_disabled_next_control"}

        previous_page = self._current_page_number()
        if previous_page is None or expected_page != previous_page + 1:
            raise RuntimeError("YAPO_PAGINATION_CURRENT_PAGE_UNVERIFIED")
        previous_html = self._page.content()
        previous_records, _, _ = _extract_listing_urls_detailed(previous_html, self._page.url)
        previous_ids = {item["url"] for item in previous_records}
        request_offset = len(self._request_events)
        try:
            button.click(timeout=self._timeout_ms)
            self._page.wait_for_function(
                """expected => {
                    const nav = document.querySelector('nav[aria-label*="pagin" i]');
                    const current = nav && nav.querySelector('[aria-current="page"]');
                    const label = current && (current.getAttribute('aria-label') || current.textContent || '');
                    const match = label.match(/\\d+/);
                    return match && Number(match[0]) === expected;
                }""",
                arg=expected_page,
                timeout=self._timeout_ms,
            )
            self._wait_for_network_quiet()
        except Exception as exc:
            raise RuntimeError(f"YAPO_PAGINATION_CLICK_FAILED: {type(exc).__name__}.") from None

        html = self._page.content()
        current_page = self._current_page_number()
        if current_page != expected_page:
            raise RuntimeError("YAPO_PAGINATION_PAGE_NUMBER_MISMATCH")
        records, _, _ = _extract_listing_urls_detailed(html, self._page.url)
        if not records or {item["url"] for item in records} == previous_ids:
            raise RuntimeError("YAPO_PAGINATION_LISTINGS_DID_NOT_CHANGE")

        observed = self._request_events[request_offset:]
        return {
            "terminal": False,
            "evidence": "client_side_button_click",
            "url": _normalize_url(self._page.url),
            "html": html,
            "request_urls": [
                {"method": item["method"], "url": item["url"]}
                for item in observed
                if item["resource_type"] in {"document", "fetch", "xhr"}
            ][:10],
            "page_number": current_page,
        }

    def close(self) -> None:
        browser = getattr(self, "_browser", None)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
            self._browser = None
        playwright = getattr(self, "_playwright", None)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
            self._playwright = None


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
    report.update({"pages": [], "pagination_complete": True, "pagination_cause": "", "errors": [], "proxy_requests": 0, "request_attempts": 0, "successful_downloads": 0, "cache_responses": 0, "pagination_requests": []})
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
        browser_navigator: _YapoClientPagination | None = None
        pending_browser_page: dict[str, Any] | None = None
        while True:
            if current_url in visited_page_urls and pending_browser_page is None:
                report["pagination_complete"] = False
                report["pagination_cause"] = "pagination_url_cycle"
                break
            visited_page_urls.add(current_url)
            if max_pages is not None and page_number > max_pages:
                report["pagination_complete"] = False
                report["pagination_cause"] = "safety_max_pages_reached"
                break
            if pending_browser_page is not None:
                browser_page = pending_browser_page
                pending_browser_page = None
                cache_key_url = browser_page["url"]
                if cache_key_url == current_url:
                    # The UI may paginate without changing location. This suffix
                    # is only a local cache identity; it is never requested.
                    cache_key_url = f"{current_url}#yapo-client-page={page_number}"
                html_path = html_path_for_url(cache_key_url, config, batch_id=batch_id)
                html_path.parent.mkdir(parents=True, exist_ok=True)
                if not html_path.exists():
                    html_path.write_text(browser_page["html"], encoding="utf-8")
                validation = validate_html(browser_page["html"])
                download = DownloadResult(
                    url=browser_page["url"],
                    html=browser_page["html"],
                    status_code=200,
                    fetch_source="playwright_proxy",
                    html_path=html_path,
                    validation_status=validation["status"],
                    validation_reason=validation["reason"],
                )
                report["proxy_requests"] += max(1, len(browser_page.get("request_urls") or []))
                report["request_attempts"] += max(1, len(browser_page.get("request_urls") or []))
            else:
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
            elif download.fetch_source in {"requests_proxy", "playwright_proxy"}:
                report["successful_downloads"] += 1
                if download.fetch_source == "requests_proxy":
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
                    if browser_navigator is not None:
                        browser_navigator.close()
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
            if evidence == "pagination_control_without_explicit_url" and _has_client_side_pagination(download.html):
                if max_pages is not None and page_number >= max_pages:
                    report["pagination_complete"] = False
                    report["pagination_cause"] = "safety_max_pages_reached"
                    break
                try:
                    if browser_navigator is None:
                        browser_navigator = _YapoClientPagination(
                            current_url,
                            download.html,
                            config,
                            proxy_manager,
                        )
                    browser_page = browser_navigator.advance(page_number + 1)
                except Exception as exc:
                    report["pagination_complete"] = False
                    report["pagination_cause"] = "client_side_pagination_error"
                    report["errors"].append(str(exc))
                    break
                if browser_page.get("terminal"):
                    report["pages"][-1]["pagination_evidence"] = browser_page.get("evidence", "explicit_disabled_next_control")
                    report["pagination_cause"] = str(browser_page.get("evidence") or "explicit_disabled_next_control")
                    break
                if not browser_page.get("html") or browser_page.get("page_number") != page_number + 1:
                    report["pagination_complete"] = False
                    report["pagination_cause"] = str(browser_page.get("evidence") or "client_side_page_unverified")
                    break
                browser_next_url = _normalize_url(str(browser_page.get("url") or current_url))
                if urlparse(browser_next_url).hostname not in {"www.yapo.cl", "yapo.cl"}:
                    report["pagination_complete"] = False
                    report["pagination_cause"] = "unauthorized_pagination_domain"
                    report["errors"].append("client-side pagination left the authorized Yapo domain")
                    break
                if browser_next_url != current_url and browser_next_url in visited_page_urls:
                    report["pagination_complete"] = False
                    report["pagination_cause"] = "pagination_url_cycle"
                    break
                report["pages"][-1]["pagination_evidence"] = str(browser_page.get("evidence") or "client_side_button_click")
                report["pages"][-1]["pagination_request_urls"] = browser_page.get("request_urls") or []
                report["pagination_requests"].extend(browser_page.get("request_urls") or [])
                current_url = browser_next_url
                pending_browser_page = browser_page
                page_number += 1
                continue
            report["pagination_complete"] = False
            report["pagination_cause"] = evidence
            break
        if browser_navigator is not None:
            browser_navigator.close()
    if not report["pagination_cause"]:
        report["pagination_cause"] = "verified_end"
    return discovered


if __name__ == "__main__":
    assert is_listing_detail_url("https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/atencion-familias/32588092")
    assert is_listing_detail_url("/bienes-raices-venta-de-propiedades-apartamentos/departamento-en-venta/32464287")
    assert not is_listing_detail_url("https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana")
    print("All tests passed.")
