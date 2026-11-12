"""Read-only verification of Prop360 inventory and public listing price."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .price_drop_campaign_discovery import PROPERTY_COLLECTION, _published, parse_aware_datetime


DEFAULT_PUBLIC_HOSTS = {
    "procasa.cl", "yapo.cl", "portalinmobiliario.com", "mercadolibre.cl",
    "toctoc.com", "chilepropiedades.cl",
}
MAX_HTML_BYTES = 2_000_000


@dataclass(frozen=True)
class PublicationCheck:
    status: str
    sendable: bool
    property_code: str
    url: str = ""
    http_status: int | None = None
    inventory_price: int | float | None = None
    inventory_currency: str = ""
    public_price: int | float | None = None
    public_currency: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _currency(value: Any) -> str:
    raw = str(value or "").strip().upper().replace(" ", "")
    if raw in {"UF", "U.F.", "CLF", "UFS"}:
        return "UF"
    if raw in {"CLP", "CL$", "$", "PESO", "PESOS"}:
        return "CLP"
    return ""


def _parse_price(raw: Any, currency: str) -> Decimal | None:
    text = str(raw or "").strip()
    if not text:
        return None
    text = re.sub(r"(?i)\b(?:CLP|CL\$|UF|U\.F\.)\b", "", text)
    text = text.replace("$", "").replace("\xa0", "").replace(" ", "")
    if currency == "CLP":
        text = re.sub(r"[^0-9]", "", text)
        return _decimal(text)
    if currency == "UF":
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
            text = text.replace(".", "")
        return _decimal(text)
    return None


def _walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def extract_public_prices(html: str, *, operation: str = "venta") -> set[tuple[str, Decimal]]:
    """Return unambiguous structured or clearly labeled visible prices."""
    soup = BeautifulSoup(html or "", "html.parser")
    found: set[tuple[str, Decimal]] = set()

    def add(raw_price: Any, raw_currency: Any) -> None:
        code = _currency(raw_currency)
        price = _parse_price(raw_price, code)
        if code and price is not None and price > 0:
            found.add((code, price))

    for tag in soup.find_all("meta"):
        key = str(tag.get("property") or tag.get("name") or tag.get("itemprop") or "").casefold()
        content = tag.get("content")
        if key in {"product:price:amount", "og:price:amount", "price", "priceamount"}:
            sibling_currency = next((
                node.get("content") for node in soup.find_all("meta")
                if str(node.get("property") or node.get("name") or node.get("itemprop") or "").casefold()
                in {"product:price:currency", "og:price:currency", "pricecurrency"}
            ), "")
            add(content, sibling_currency)

    for node in soup.find_all(attrs={"itemprop": "price"}):
        raw_price = node.get("content") or node.get("value") or node.get_text(" ", strip=True)
        currency_node = soup.find(attrs={"itemprop": "priceCurrency"})
        raw_currency = (currency_node.get("content") or currency_node.get_text(" ", strip=True)
                        if currency_node else "")
        add(raw_price, raw_currency)

    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        for node in _walk_json(payload):
            if "price" in node:
                add(node.get("price"), node.get("priceCurrency") or node.get("currency"))

    # PROCASA may omit structured metadata but label the asking amount
    # "Venta - <type> - <price>" or "Arriendo - <type> - <price>".
    # Never attribute a sale price to a rental (or vice versa).
    visible_text = soup.get_text(" ", strip=True)
    heading_operation = "arriendo" if operation == "arriendo" else "venta"
    visible_heading_price = re.compile(
        rf"\b{heading_operation}\s*[-–—]\s*[^|,;–—-]{{1,80}}?\s*[-–—]\s*"
        r"(?P<price>(?:UF|U\.F\.|CLP)\s*\$?\s*\d[\d.,]*"
        r"|\$\s*\d[\d.,]*"
        r"|\d[\d.,]*\s*(?:UF|U\.F\.|CLP|pesos?))",
        re.I,
    )
    for match in visible_heading_price.finditer(visible_text):
        phrase = match.group("price")
        currency_match = re.search(r"(?i)U\.F\.|UF|CLP|CL\$|pesos?", phrase)
        currency_marker = currency_match.group(0) if currency_match else "$"
        amount = re.sub(r"(?i)U\.F\.|UF|CLP|CL\$|pesos?", "", phrase)
        amount = amount.replace("$", "").strip()
        add(amount, currency_marker)
    return found


def _allowed_hosts() -> set[str]:
    configured = os.getenv("PRICE_DROP_PUBLIC_HOST_ALLOWLIST", "").strip()
    hosts = {part.strip().lower().lstrip(".") for part in configured.split(",") if part.strip()}
    return hosts or DEFAULT_PUBLIC_HOSTS


def _host_allowed(url: str, allowed: set[str]) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        return parsed.scheme == "https" and any(
            host == item or host.endswith("." + item) for item in allowed
        )
    except ValueError:
        return False


def _fetch_public_html(url: str, *, get: Callable[..., Any],
                       allowed_hosts: set[str]) -> tuple[str, int, str]:
    current = url
    response = None
    for _ in range(4):
        if not _host_allowed(current, allowed_hosts):
            return "", 0, "public_host_not_allowlisted"
        try:
            response = get(
                current, timeout=(3, 8), allow_redirects=False, stream=True,
                headers={"User-Agent": "PROCASA-PriceDropVerifier/1.0"},
            )
        except Exception:
            return "", 0, "public_listing_request_failed"
        status = int(getattr(response, "status_code", 0) or 0)
        if status in {301, 302, 303, 307, 308}:
            target = urljoin(current, str(getattr(response, "headers", {}).get("Location") or ""))
            close = getattr(response, "close", None)
            if callable(close):
                close()
            if not target or target == current:
                return "", status, "public_listing_redirect_invalid"
            current = target
            continue
        if status != 200:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            return "", status, "public_listing_http_error"
        iter_content = getattr(response, "iter_content", None)
        if callable(iter_content):
            chunks, total = [], 0
            try:
                for chunk in iter_content(chunk_size=32_768):
                    if not chunk:
                        continue
                    chunk = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                    remaining = MAX_HTML_BYTES - total
                    if remaining <= 0:
                        break
                    chunks.append(chunk[:remaining])
                    total += min(len(chunk), remaining)
            except Exception:
                return "", status, "public_listing_response_read_failed"
            body = b"".join(chunks).decode("utf-8", errors="replace")
        else:
            content = getattr(response, "content", None)
            if content is not None:
                raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
                body = raw[:MAX_HTML_BYTES].decode("utf-8", errors="replace")
            else:
                body = str(getattr(response, "text", ""))[:MAX_HTML_BYTES]
        close = getattr(response, "close", None)
        if callable(close):
            close()
        return body, status, ""
    return "", 0, "public_listing_redirect_limit"


def verify_current_listing(db: Any, *, property_code: str, currency: str,
                           expected_price: int | float, expected_url: str = "",
                           now: datetime | None = None,
                           http_get: Callable[..., Any] = requests.get,
                           operation: str = "venta") -> PublicationCheck:
    """Verify current Prop360 state and, when reachable, the public list price.

    Non-verified states are deliberately not sendable. Preview callers can show
    the reason; send callers must re-run the same check immediately before the
    individual provider attempt.
    """
    code = str(property_code or "")
    currency = _currency(currency)
    expected = _decimal(expected_price)
    stamp = now or datetime.now(timezone.utc)
    prop = db[PROPERTY_COLLECTION].find_one(
        {"codigo": code},
        {"_id": 0, "codigo": 1, "disponible_prop360": 1, "estado.oficina": 1,
         "tipo_operacion": 1, "publicaciones": 1},
    ) or {}
    operation = str(operation or "").strip().lower()
    if operation not in {"venta", "arriendo"}:
        return PublicationCheck(
            status="inventory_public_inconsistent", sendable=False,
            property_code=code, reason="unsupported_property_operation",
        )
    operation_data = prop.get("tipo_operacion") or {}
    pricing = operation_data.get(
        "precio_venta" if operation == "venta" else "precio_arriendo"
    ) or {}
    inventory_currency = _currency(
        pricing.get("moneda_publicada") or pricing.get("moneda_publicacion")
    )
    inventory_price = _decimal(pricing.get("precio_publicado"))

    def result(status: str, sendable: bool = False, *, url: str = "", http_status=None,
               public_price=None, public_currency="", reason="") -> PublicationCheck:
        return PublicationCheck(
            status=status, sendable=sendable, property_code=code, url=url,
            http_status=http_status,
            inventory_price=(int(inventory_price) if inventory_price is not None and inventory_price == inventory_price.to_integral_value()
                             else float(inventory_price) if inventory_price is not None else None),
            inventory_currency=inventory_currency,
            public_price=(int(public_price) if public_price is not None and public_price == public_price.to_integral_value()
                          else float(public_price) if public_price is not None else None),
            public_currency=public_currency, reason=reason,
        )

    if (prop.get("disponible_prop360") is not True
            or operation_data.get(operation) is not True):
        return result("property_removed",
                      reason=f"prop360_property_not_available_for_{operation}")
    if (prop.get("estado") or {}).get("oficina") != "PROCASA SUCRE":
        return result("inventory_public_inconsistent", reason="property_office_changed")
    if not expected or not currency or inventory_currency != currency or inventory_price != expected:
        return result("inventory_public_inconsistent", reason="prop360_price_or_currency_changed")
    published, current_url = _published(prop, stamp, operation=operation)
    if not published:
        return result("property_removed", reason="no_active_prop360_publication")
    if expected_url and expected_url != current_url:
        return result("inventory_public_inconsistent", url=current_url,
                      reason="active_listing_url_changed_after_preview")

    html, http_status, fetch_reason = _fetch_public_html(
        current_url, get=http_get, allowed_hosts=_allowed_hosts(),
    )
    if fetch_reason:
        if http_status in {404, 410}:
            return result("property_removed", url=current_url, http_status=http_status,
                          reason="public_listing_not_found")
        return result("link_inaccessible", url=current_url, http_status=http_status,
                      reason=fetch_reason)
    lowered = html.casefold()
    if any(marker in lowered for marker in (
        "publicación eliminada", "publicacion eliminada", "propiedad no encontrada",
        "anuncio eliminado", "anuncio borrado", "listing not found",
    )):
        return result("property_removed", url=current_url, http_status=http_status,
                      reason="public_page_reports_removed_listing")

    prices = extract_public_prices(html, operation=operation)
    if not prices:
        return result("link_accessible_price_unverifiable", url=current_url,
                      http_status=http_status, reason="no_structured_public_price_found")
    if len(prices) != 1:
        return result("link_accessible_price_unverifiable", url=current_url,
                      http_status=http_status, reason="public_price_metadata_ambiguous")
    public_currency, public_price = next(iter(prices))
    if public_currency != currency or public_price != expected:
        return result("inventory_public_inconsistent", url=current_url,
                      http_status=http_status, public_price=public_price,
                      public_currency=public_currency, reason="public_price_differs_from_prop360")
    return result("price_verified", True, url=current_url, http_status=http_status,
                  public_price=public_price, public_currency=public_currency)
