"""Owner-focused, read-only Toctoc discovery.

This module deliberately stops at ``client.idType``.  It does not classify
with DeepSeek, persist captaciones in MongoDB, create assignments, or call the
CRM.  The only persistent state is a small local JSON cursor/cache used to
resume the discovery safely.

The browser source is kept behind a protocol so the crawler can be tested with
deterministic fixtures and so a future BFF/SEO adapter can be introduced
without changing cursor, cache, or prioritisation semantics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import urlencode


SOURCE_VERSION = "owner-discovery-v1"
STATE_VERSION = 1
IDTYPE_OWNER = "1"
IDTYPE_BROKER = "2"
IDTYPE_NEW_DEVELOPMENT = "3"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def stable_fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OwnerDiscoverySegment:
    region: str
    commune: str
    property_type: str
    operation: str = "venta"

    @property
    def key(self) -> str:
        return "|".join((self.operation, self.region, self.commune, self.property_type))


@dataclass(slots=True)
class SegmentCursor:
    last_page_scanned: int = 0
    last_card_position: int = 0
    pages_scanned: int = 0
    details_requested: int = 0
    owners_found: int = 0
    brokers_found: int = 0
    new_developments_found: int = 0
    owner_yield: float = 0.0
    last_run_at: str = ""
    stop_reason: str = ""


@dataclass(slots=True)
class DiscoveryLimits:
    max_pages_per_segment: int = 20
    no_new_id_pages: int = 2
    error_threshold: int = 3
    max_detail_requests: int | None = None
    max_detail_requests_per_segment: int | None = None
    page_wait_ms: int = 700
    detail_wait_ms: int = 350


@dataclass(slots=True)
class DiscoveryRunResult:
    run_id: str
    cards_discovered: int = 0
    details_requested: int = 0
    details_skipped_known: int = 0
    owners_found: int = 0
    brokers_found: int = 0
    new_developments_found: int = 0
    unknown_found: int = 0
    owner_candidates: list[dict[str, Any]] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    owners_by_region: dict[str, int] = field(default_factory=dict)
    owners_by_commune: dict[str, int] = field(default_factory=dict)
    owners_by_property_type: dict[str, int] = field(default_factory=dict)
    owners_by_page_band: dict[str, int] = field(default_factory=dict)
    stop_reasons: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def detail_requests_per_owner(self) -> float:
        return round(self.details_requested / self.owners_found, 3) if self.owners_found else 0.0

    @property
    def detail_request_reduction_percent(self) -> float:
        total = self.details_requested + self.details_skipped_known
        return round((self.details_skipped_known / total) * 100, 2) if total else 0.0

    def as_report(self) -> dict[str, Any]:
        result = asdict(self)
        result["detail_requests_per_owner"] = self.detail_requests_per_owner
        result["detail_request_reduction_percent"] = self.detail_request_reduction_percent
        result["deepseek_calls"] = 0
        result["tokens_used"] = 0
        result["property_assignments"] = 0
        return result


class OwnerDiscoverySource(Protocol):
    def list_cards(self, segment: OwnerDiscoverySegment, page: int) -> list[dict[str, Any]]:
        """Return cards with at least ``url`` or ``listing_id``."""

    def get_detail(self, url: str) -> dict[str, Any]:
        """Return parsed detail data, including the raw idType when available."""

    def close(self) -> None:
        """Release browser/network resources."""


class OwnerDiscoveryStore:
    """Local durable state; never a Mongo property collection."""

    def __init__(self, state_path: str | Path):
        self.path = Path(state_path)
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = {}
        else:
            loaded = {}
        self.state: dict[str, Any] = {
            "version": STATE_VERSION,
            "source_version": loaded.get("source_version", SOURCE_VERSION),
            "cursors": loaded.get("cursors", {}) if isinstance(loaded.get("cursors", {}), dict) else {},
            "idtype_cache": loaded.get("idtype_cache", {}) if isinstance(loaded.get("idtype_cache", {}), dict) else {},
            "yield_stats": loaded.get("yield_stats", {}) if isinstance(loaded.get("yield_stats", {}), dict) else {},
        }

    def save(self) -> None:
        _atomic_write_json(self.path, self.state)

    def cursor(self, segment: OwnerDiscoverySegment) -> SegmentCursor:
        raw = self.state["cursors"].get(segment.key, {})
        allowed = {field.name for field in SegmentCursor.__dataclass_fields__.values()}
        return SegmentCursor(**{key: value for key, value in raw.items() if key in allowed})

    def save_cursor(self, segment: OwnerDiscoverySegment, cursor: SegmentCursor) -> None:
        cursor.last_run_at = utc_now()
        self.state["cursors"][segment.key] = asdict(cursor)
        self.save()

    def cached(self, listing_id: str, source_version: str = SOURCE_VERSION) -> dict[str, Any] | None:
        value = self.state["idtype_cache"].get(str(listing_id))
        if not isinstance(value, dict) or value.get("source_version") != source_version:
            return None
        return dict(value)

    def save_cache(self, listing_id: str, value: dict[str, Any]) -> None:
        self.state["idtype_cache"][str(listing_id)] = dict(value)
        self.save()

    def yield_for(self, segment: OwnerDiscoverySegment, page_band: str) -> float:
        raw = self.state["yield_stats"].get(f"{segment.key}|{page_band}", {})
        try:
            return float(raw.get("owner_yield", 0.0))
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def yield_for_segment(self, segment: OwnerDiscoverySegment) -> float:
        prefix = f"{segment.key}|"
        details = 0
        owners = 0
        for key, raw in self.state["yield_stats"].items():
            if not key.startswith(prefix) or not isinstance(raw, dict):
                continue
            details += int(raw.get("details", 0) or 0)
            owners += int(raw.get("owners", 0) or 0)
        return (owners / details) if details else 0.0

    def save_yield(
        self,
        segment: OwnerDiscoverySegment,
        page_band: str,
        *,
        details: int,
        owners: int,
        pages: int,
    ) -> None:
        key = f"{segment.key}|{page_band}"
        previous = self.state["yield_stats"].get(key, {})
        total_details = int(previous.get("details", 0)) + details
        total_owners = int(previous.get("owners", 0)) + owners
        total_pages = int(previous.get("pages", 0)) + pages
        self.state["yield_stats"][key] = {
            "details": total_details,
            "owners": total_owners,
            "pages": total_pages,
            "owner_yield": (total_owners / total_details) if total_details else 0.0,
            "last_run_at": utc_now(),
        }
        self.save()


def page_band(page: int) -> str:
    start = ((max(page, 1) - 1) // 5) * 5 + 1
    return f"{start}-{start + 4}"


def listing_id_from_card(card: dict[str, Any]) -> str:
    value = card.get("listing_id") or card.get("id") or card.get("idProperty")
    if value not in (None, ""):
        return str(value)
    url = str(card.get("url") or card.get("urlFicha") or "")
    try:
        from discovery import listing_id_from_url, listing_id_from_url_fallback
        discovered, _ = listing_id_from_url(url)
        if discovered:
            return discovered
        return listing_id_from_url_fallback(url) if url else ""
    except Exception:
        pass
    match = re.search(r"/(\d{5,8})(?:[/?#]|$)", url)
    return match.group(1) if match else ""


def card_url(card: dict[str, Any]) -> str:
    return str(card.get("url") or card.get("urlFicha") or "")


def normalize_id_type(detail: dict[str, Any]) -> str:
    client = detail.get("client") if isinstance(detail.get("client"), dict) else {}
    value = (
        detail.get("seller_id_type_raw")
        or detail.get("seller_id_type")
        or detail.get("idType")
        or detail.get("id_type")
        or client.get("idType")
        or client.get("id_type")
    )
    return str(value).strip() if value not in (None, "") else ""


def classify_id_type(raw: str) -> str:
    return {
        IDTYPE_OWNER: "OWNER_CANDIDATE",
        IDTYPE_BROKER: "BROKER",
        IDTYPE_NEW_DEVELOPMENT: "OUT_OF_SCOPE_NEW_DEVELOPMENT",
    }.get(str(raw).strip(), "UNKNOWN")


def normalize_detail(
    detail: dict[str, Any],
    *,
    listing_id: str,
    url: str,
    segment: OwnerDiscoverySegment,
    card: dict[str, Any],
    source_version: str = SOURCE_VERSION,
) -> dict[str, Any]:
    client = detail.get("client") if isinstance(detail.get("client"), dict) else {}
    raw_id_type = normalize_id_type(detail)
    publisher = (
        detail.get("publicador_visible")
        or detail.get("publisher")
        or detail.get("seller_name")
        or client.get("name")
        or card.get("publisher")
        or ""
    )
    client_id = (
        detail.get("seller_client_id")
        or detail.get("client_id")
        or client.get("clientId")
        or client.get("client_id")
        or client.get("id")
        or ""
    )
    profile_id = (
        detail.get("seller_profile_id")
        or detail.get("profile_id")
        or client.get("profileId")
        or client.get("profile_id")
        or client.get("idProfile")
        or ""
    )
    normalized = {
        "listing_id": str(listing_id),
        "url": url,
        "seller_id_type_raw": raw_id_type,
        "classification": classify_id_type(raw_id_type),
        "publisher": str(publisher or ""),
        "client_id": str(client_id or ""),
        "profile_id": str(profile_id or ""),
        "profile_url": str(detail.get("seller_profile_url") or detail.get("profile_url") or client.get("url") or ""),
        "profile_logo": str(detail.get("seller_profile_logo") or detail.get("profile_logo") or client.get("logo") or ""),
        "region": str(detail.get("region") or segment.region),
        "commune": str(detail.get("comuna") or detail.get("commune") or card.get("comuna") or segment.commune),
        "property_type": str(detail.get("tipo_propiedad") or detail.get("property_type") or segment.property_type),
        "operation": str(detail.get("operacion") or detail.get("operation") or segment.operation),
        "title": str(detail.get("title") or card.get("title") or ""),
        "first_seen": str(detail.get("first_seen") or utc_now()),
        "last_seen": utc_now(),
        "source_version": source_version,
    }
    normalized["raw_fingerprint"] = stable_fingerprint({
        key: normalized[key]
        for key in ("url", "seller_id_type_raw", "publisher", "client_id", "profile_id", "title")
    })
    return normalized


def load_segments(path: str | Path) -> list[OwnerDiscoverySegment]:
    """Load national scope from configuration; never invent a Santiago scope."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("segments") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("OWNER_DISCOVERY_SEGMENTS_MUST_BE_LIST")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        required = (row.get("region"), row.get("commune") or row.get("comuna"), row.get("property_type") or row.get("tipo_propiedad"))
        if not all(required):
            raise ValueError("OWNER_DISCOVERY_SEGMENT_MISSING_FIELD")
        result.append(OwnerDiscoverySegment(
            region=str(required[0]),
            commune=str(required[1]),
            property_type=str(required[2]),
            operation=str(row.get("operation") or row.get("operacion") or "venta"),
        ))
    if not result:
        raise ValueError("OWNER_DISCOVERY_SEGMENTS_EMPTY")
    return result


class OwnerDiscoveryCrawler:
    def __init__(
        self,
        source: OwnerDiscoverySource,
        store: OwnerDiscoveryStore,
        *,
        limits: DiscoveryLimits | None = None,
        source_version: str = SOURCE_VERSION,
        run_id_factory: Callable[[], str] | None = None,
    ):
        self.source = source
        self.store = store
        self.limits = limits or DiscoveryLimits()
        self.source_version = source_version
        self.run_id_factory = run_id_factory or (lambda: f"owner_discovery_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}" )

    def prioritize_segments(self, segments: Iterable[OwnerDiscoverySegment]) -> list[OwnerDiscoverySegment]:
        indexed = list(enumerate(segments))
        return [segment for _, segment in sorted(
            indexed,
            key=lambda item: (-self.store.yield_for_segment(item[1]), item[0]),
        )]

    def run(self, segments: Iterable[OwnerDiscoverySegment], *, reset: bool = False) -> DiscoveryRunResult:
        run = DiscoveryRunResult(run_id=self.run_id_factory())
        run_seen: set[str] = set()
        try:
            for segment in self.prioritize_segments(segments):
                if reset:
                    self.store.state["cursors"].pop(segment.key, None)
                    self.store.save()
                self._run_segment(segment, run, run_seen)
        finally:
            self.source.close()
        return run

    def _run_segment(self, segment: OwnerDiscoverySegment, run: DiscoveryRunResult, run_seen: set[str]) -> None:
        cursor = self.store.cursor(segment)
        start_page = cursor.last_page_scanned + 1
        no_new_streak = 0
        errors = 0
        segment_report: dict[str, Any] = {
            "segment": asdict(segment),
            "start_page": start_page,
            "pages": 0,
            "cards": 0,
            "details_requested": 0,
            "details_skipped_known": 0,
            "stop_reason": "",
        }
        for page in range(start_page, start_page + self.limits.max_pages_per_segment):
            if self.limits.max_detail_requests is not None and run.details_requested >= self.limits.max_detail_requests:
                cursor.stop_reason = "REQUEST_BUDGET_EXCEEDED"
                segment_report["stop_reason"] = cursor.stop_reason
                break
            if (
                self.limits.max_detail_requests_per_segment is not None
                and segment_report["details_requested"] >= self.limits.max_detail_requests_per_segment
            ):
                cursor.stop_reason = "SEGMENT_DETAIL_BUDGET_EXCEEDED"
                segment_report["stop_reason"] = cursor.stop_reason
                break
            try:
                cards = self.source.list_cards(segment, page) or []
            except Exception as exc:
                errors += 1
                run.errors.append({"segment": segment.key, "page": page, "error": str(exc)[:500]})
                if errors >= self.limits.error_threshold:
                    cursor.stop_reason = "ERROR_THRESHOLD"
                    segment_report["stop_reason"] = cursor.stop_reason
                    break
                continue
            cursor.pages_scanned += 1
            segment_report["pages"] += 1
            run.cards_discovered += len(cards)
            segment_report["cards"] += len(cards)
            page_ids: set[str] = set()
            new_ids = 0
            for position, card in enumerate(cards, start=1):
                listing_id = listing_id_from_card(card)
                url = card_url(card)
                if not listing_id or not url or listing_id in page_ids or listing_id in run_seen:
                    continue
                page_ids.add(listing_id)
                run_seen.add(listing_id)
                new_ids += 1
                cursor.last_card_position = position
                cached = self.store.cached(listing_id, self.source_version)
                if cached:
                    run.details_skipped_known += 1
                    segment_report["details_skipped_known"] += 1
                    self._record_classification(run, cached, from_cache=True, page=page, segment=segment)
                    self.store.save_cursor(segment, cursor)
                    continue
                if self.limits.max_detail_requests is not None and run.details_requested >= self.limits.max_detail_requests:
                    cursor.stop_reason = "REQUEST_BUDGET_EXCEEDED"
                    break
                if (
                    self.limits.max_detail_requests_per_segment is not None
                    and segment_report["details_requested"] >= self.limits.max_detail_requests_per_segment
                ):
                    cursor.stop_reason = "SEGMENT_DETAIL_BUDGET_EXCEEDED"
                    break
                detail = self.source.get_detail(url) or {}
                run.details_requested += 1
                segment_report["details_requested"] += 1
                cursor.details_requested += 1
                normalized = normalize_detail(
                    detail,
                    listing_id=listing_id,
                    url=url,
                    segment=segment,
                    card=card,
                    source_version=self.source_version,
                )
                self.store.save_cache(listing_id, normalized)
                classification = self._record_classification(run, normalized, from_cache=False, page=page, segment=segment)
                if classification == "OWNER_CANDIDATE":
                    cursor.owners_found += 1
                elif classification == "BROKER":
                    cursor.brokers_found += 1
                elif classification == "OUT_OF_SCOPE_NEW_DEVELOPMENT":
                    cursor.new_developments_found += 1
                cursor.last_card_position = position
                self.store.save_cursor(segment, cursor)
            if cursor.stop_reason in {"REQUEST_BUDGET_EXCEEDED", "SEGMENT_DETAIL_BUDGET_EXCEEDED"}:
                segment_report["stop_reason"] = cursor.stop_reason
                break
            cursor.last_page_scanned = page
            if not cards:
                cursor.stop_reason = "END_OF_RESULTS"
                segment_report["stop_reason"] = cursor.stop_reason
                self.store.save_cursor(segment, cursor)
                break
            if new_ids == 0:
                no_new_streak += 1
            else:
                no_new_streak = 0
            self.store.save_yield(
                segment,
                page_band(page),
                details=segment_report["details_requested"],
                owners=sum(1 for item in run.owner_candidates if item.get("segment_key") == segment.key and item.get("page") == page),
                pages=1,
            )
            if no_new_streak >= self.limits.no_new_id_pages:
                cursor.stop_reason = "NO_NEW_IDS"
                segment_report["stop_reason"] = cursor.stop_reason
                self.store.save_cursor(segment, cursor)
                break
            if page >= start_page + self.limits.max_pages_per_segment - 1:
                cursor.stop_reason = "MAX_PAGES_REACHED"
                segment_report["stop_reason"] = cursor.stop_reason
                self.store.save_cursor(segment, cursor)
                break
        if not segment_report["stop_reason"]:
            segment_report["stop_reason"] = cursor.stop_reason or "COMPLETED"
        if cursor.details_requested:
            cursor.owner_yield = cursor.owners_found / cursor.details_requested
        cursor.stop_reason = segment_report["stop_reason"]
        self.store.save_cursor(segment, cursor)
        run.stop_reasons[segment_report["stop_reason"]] = run.stop_reasons.get(segment_report["stop_reason"], 0) + 1
        run.segments.append(segment_report)

    def _record_classification(
        self,
        run: DiscoveryRunResult,
        item: dict[str, Any],
        *,
        from_cache: bool,
        page: int,
        segment: OwnerDiscoverySegment,
    ) -> str:
        classification = classify_id_type(str(item.get("seller_id_type_raw") or ""))
        if classification == "OWNER_CANDIDATE":
            run.owners_found += 1
            run.owners_by_region[item.get("region", "")] = run.owners_by_region.get(item.get("region", ""), 0) + 1
            run.owners_by_commune[item.get("commune", "")] = run.owners_by_commune.get(item.get("commune", ""), 0) + 1
            run.owners_by_property_type[item.get("property_type", "")] = run.owners_by_property_type.get(item.get("property_type", ""), 0) + 1
            band = page_band(page)
            run.owners_by_page_band[band] = run.owners_by_page_band.get(band, 0) + 1
            # A cached listing is still a candidate, but never creates a
            # second candidate row in the same run.
            if not any(row.get("listing_id") == item.get("listing_id") for row in run.owner_candidates):
                candidate = dict(item)
                candidate["decision_source"] = "IDTYPE_CACHE" if from_cache else "TOCTOC_DETAIL_NEXTDATA"
                candidate["segment_key"] = segment.key
                candidate["page"] = page
                run.owner_candidates.append(candidate)
        elif classification == "BROKER":
            run.brokers_found += 1
        elif classification == "OUT_OF_SCOPE_NEW_DEVELOPMENT":
            run.new_developments_found += 1
        else:
            run.unknown_found += 1
        return classification


class PlaywrightOwnerDiscoverySource:
    """Live geographic search/detail adapter; no Mongo or HTML dump writes."""

    def __init__(self, base_url: str = "https://www.toctoc.com", *, headless: bool = True, wait_ms: int = 1000, scroll_rounds: int = 6):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.wait_ms = wait_ms
        self.scroll_rounds = max(1, scroll_rounds)
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def _ensure(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(locale="es-CL")
        self._page = self._context.new_page()

    @staticmethod
    def _is_detail_url(url: str) -> bool:
        try:
            from discovery import is_listing_detail_url
            return bool(is_listing_detail_url(url))
        except Exception:
            low = url.lower()
            return "toctoc.com/propiedades/" in low or "toctoc.com/propiedad/" in low or "/venta/" in low or "/arriendo/" in low

    def list_cards(self, segment: OwnerDiscoverySegment, page: int) -> list[dict[str, Any]]:
        self._ensure()
        route_commune = "santiago" if segment.commune == "santiago-centro" else segment.commune
        query = urlencode({"pagina": page})
        url = f"{self.base_url}/{segment.operation}/{segment.property_type}/{segment.region}/{route_commune}?{query}"
        self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        selector = 'a[href*="/propiedad/"], a[href*="/propiedades/"], a[href*="/venta/"], a[href*="/arriendo/"]'
        try:
            self._page.wait_for_selector(selector, timeout=10000)
        except Exception:
            pass
        hrefs: list[str] = []
        previous_count = -1
        for _ in range(self.scroll_rounds):
            current = self._page.locator("a").evaluate_all("els => els.map(a => a.href || a.getAttribute('href') || '')")
            hrefs = list(dict.fromkeys([str(value) for value in current if value]))
            detail_count = sum(1 for value in hrefs if self._is_detail_url(value))
            if detail_count == previous_count:
                break
            previous_count = detail_count
            self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self._page.wait_for_timeout(self.wait_ms)
        seen: set[str] = set()
        cards: list[dict[str, Any]] = []
        for href in hrefs:
            href = str(href or "").split("#", 1)[0]
            if not self._is_detail_url(href) or href in seen:
                continue
            seen.add(href)
            listing_id = listing_id_from_card({"url": href})
            if listing_id:
                cards.append({
                    "listing_id": listing_id,
                    "url": href,
                    "region": segment.region,
                    "commune": segment.commune,
                    "property_type": segment.property_type,
                })
        return cards

    def get_detail(self, url: str) -> dict[str, Any]:
        self._ensure()
        self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        self._page.wait_for_timeout(self.wait_ms)
        html = self._page.content()
        try:
            from extractor import extract_listing_fields
            return extract_listing_fields(html, source_url=url)
        except Exception:
            # Keep a minimal fallback: the crawler still records UNKNOWN and
            # never treats missing detail data as owner evidence.
            return {"raw_fingerprint": stable_fingerprint(html), "seller_id_type_raw": ""}

    def close(self) -> None:
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = self._browser = self._context = self._page = None


class _Cli:
    @staticmethod
    def main() -> int:
        parser = argparse.ArgumentParser(description="Read-only Toctoc owner discovery")
        parser.add_argument("--segments", required=True, help="JSON national segment configuration")
        parser.add_argument("--state", required=True, help="Local cursor/cache JSON path")
        parser.add_argument("--max-pages", type=int, default=20)
        parser.add_argument("--max-details", type=int, default=None)
        parser.add_argument("--reset", action="store_true")
        args = parser.parse_args()
        segments = load_segments(args.segments)
        crawler = OwnerDiscoveryCrawler(
            PlaywrightOwnerDiscoverySource(wait_ms=700),
            OwnerDiscoveryStore(args.state),
            limits=DiscoveryLimits(max_pages_per_segment=args.max_pages, max_detail_requests=args.max_details),
        )
        print(json.dumps(crawler.run(segments, reset=args.reset).as_report(), ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(_Cli.main())
