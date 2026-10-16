"""Read-only-first reconciliation of Prop360 publications for PROCASA SUCRE.

Mongo writes are restricted to the ``publicaciones`` subdocument and the
dedicated retry/checkpoint state collection.  The default command is dry-run.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from pymongo import ReturnDocument

from config import Config
from chatbot.property_lookup import (
    build_property_alias,
    canonical_portal,
    extract_property_external_id,
    normalize_property_url,
)
from chatbot.publication_data import (
    collect_publication_references,
    merge_publication_snapshot,
)
from scraping_convecta.scraping_prop360_ficha_completa import (
    OFICINAS,
    Prop360Client,
    get_mongo_collection,
    now_iso,
    parse_publicaciones_json,
)
from chatbot.sucre_portfolio_sync import (
    _acquire_lock,
    _release_lock,
    _validate_listing,
    _utc_now,
)

logger = logging.getLogger("prop360.publication_sync")

OFFICE_ID = 7
OFFICE_NAME = OFICINAS[OFFICE_ID]
COLLECTION_NAME = Config.PROPERTY_COLLECTION_NAME
STATE_COLLECTION = "prop360_publication_sync_state"
LOCK_KEY = "prop360_sucre"
DEFAULT_BATCH_SIZE = 25
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 2_000_000
PROPPIT_SOURCE = "proppit_aggregator"
PROP360_SOURCE_HOST = "procasa.prop360.cl"
PROVIDERS = ("icasas", "trovit", "mitula", "nuroa", "nestoria")
PROVIDER_FIELDS = {
    "icasas": ("icasas.cl",),
    "trovit": ("trovit.cl",),
    "mitula": ("mitula.cl",),
    "nuroa": ("nuroa.cl",),
    "nestoria": ("nestoria.cl",),
}
ALLOWED_HOSTS = (
    # Proppit redirects its trusted aggregation endpoint to proppit.com before
    # serving the same property-specific destination list.
    "links.proppit.com", "proppit.com", "icasas.cl", "trovit.cl", "mitula.cl", "nuroa.cl", "nestoria.cl",
)
_HOST_LOCKS: dict[str, threading.Lock] = {}
_HOST_LOCKS_GUARD = threading.Lock()
_HOST_LAST_REQUEST: dict[str, float] = {}
_ROBOTS_CACHE: dict[str, tuple[float, RobotFileParser | None]] = {}


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _canonical_operation(value: Any) -> str | None:
    value = str(value or "").strip().lower()
    if value in {"v", "venta", "sell"}:
        return "V"
    if value in {"a", "r", "arriendo", "rent", "alquiler"}:
        return "A"
    return None


def _office_consistency(doc: dict) -> tuple[bool, str | None]:
    """Fail closed when any persisted office identity contradicts Sucre."""
    identities = []
    if doc.get("oficina_id") is not None:
        identities.append(("oficina_id", doc.get("oficina_id") == OFFICE_ID))
    for path, value in (
        ("oficina_nombre", doc.get("oficina_nombre")),
        ("resumen.oficina", (doc.get("resumen") or {}).get("oficina")),
        ("estado.oficina", (doc.get("estado") or {}).get("oficina")),
    ):
        if value not in (None, ""):
            identities.append((path, str(value).strip().casefold() == OFFICE_NAME.casefold()))
    if not identities:
        return False, "office_identity_missing"
    conflicts = [name for name, matches in identities if not matches]
    if conflicts:
        return False, "office_identity_conflict:" + ",".join(conflicts)
    return True, None


def _active_mongo_property(doc: dict) -> bool:
    if doc.get("disponible_prop360") is not True:
        return False
    estado = doc.get("estado") if isinstance(doc.get("estado"), dict) else {}
    resumen = doc.get("resumen") if isinstance(doc.get("resumen"), dict) else {}
    status = str(estado.get("estado_prop360") or resumen.get("estado_prop360") or "").strip().casefold()
    return status in {"activa", "active"}


def _safe_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS)


def _validate_public_url(url: str) -> tuple[bool, str]:
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() != "https" or not parts.hostname:
            return False, "https_required"
        if parts.username or parts.password or parts.port not in (None, 443):
            return False, "unsafe_url_authority"
        host = parts.hostname.lower().rstrip(".")
        if not _safe_host(host):
            return False, "host_not_allowlisted"
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses:
            return False, "dns_no_addresses"
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                return False, "non_public_ip"
        return True, ""
    except (OSError, ValueError):
        return False, "dns_or_url_error"


def _rate_limit(host: str) -> None:
    host = host.lower()
    with _HOST_LOCKS_GUARD:
        lock = _HOST_LOCKS.setdefault(host, threading.Lock())
    with lock:
        wait = 1.0 - (time.monotonic() - _HOST_LAST_REQUEST.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _HOST_LAST_REQUEST[host] = time.monotonic()


def _robots_allows(url: str, http) -> tuple[bool, str]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    cached = _ROBOTS_CACHE.get(host)
    if cached and cached[0] > time.monotonic():
        parser = cached[1]
        return (parser.can_fetch("ProcasaPublicationAudit/1.0", url), "robots_disallowed") if parser else (False, "robots_unavailable")
    robots_url = f"https://{host}/robots.txt"
    valid, reason = _validate_public_url(robots_url)
    if not valid:
        _ROBOTS_CACHE[host] = (time.monotonic() + 300, None)
        return False, "robots_" + reason
    _rate_limit(host)
    try:
        response = http.get(
            robots_url, allow_redirects=False, timeout=(5, 10),
            headers={"User-Agent": "ProcasaPublicationAudit/1.0"}, stream=True,
        )
        status = int(response.status_code)
        if status in {404, 410}:
            response.close()
            parser = RobotFileParser(robots_url)
            parser.parse([])
        elif status == 200:
            chunks, size = [], 0
            for chunk in response.iter_content(4096):
                size += len(chunk)
                if size > 256_000:
                    response.close()
                    _ROBOTS_CACHE[host] = (time.monotonic() + 300, None)
                    return False, "robots_file_too_large"
                chunks.append(chunk)
            response.close()
            parser = RobotFileParser(robots_url)
            parser.parse(b"".join(chunks).decode("utf-8", errors="replace").splitlines())
        else:
            response.close()
            _ROBOTS_CACHE[host] = (time.monotonic() + 300, None)
            return False, f"robots_http_{status}"
        _ROBOTS_CACHE[host] = (time.monotonic() + 6 * 3600, parser)
        if not parser.can_fetch("ProcasaPublicationAudit/1.0", url):
            return False, "robots_disallowed"
        return True, ""
    except requests.RequestException as exc:
        _ROBOTS_CACHE[host] = (time.monotonic() + 300, None)
        return False, "robots_" + type(exc).__name__


def safe_fetch_html(url: str, *, session=None) -> dict:
    """GET an allowlisted HTTPS URL with validated DNS and manual redirects."""
    http = session or requests.Session()
    if hasattr(http, "trust_env"):
        http.trust_env = False
    current = url
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            valid, reason = _validate_public_url(current)
            if not valid:
                return {"status": "blocked", "reason": reason, "final_url": current, "html": ""}
            host = urlsplit(current).hostname or ""
            allowed, robots_reason = _robots_allows(current, http)
            if not allowed:
                return {"status": "robots_blocked", "reason": robots_reason, "final_url": current, "html": ""}
            response = None
            for attempt in range(3):
                _rate_limit(host)
                response = http.get(
                    current, allow_redirects=False, timeout=(5, 15),
                    headers={"User-Agent": "ProcasaPublicationAudit/1.0"}, stream=True,
                )
                transient_status = int(response.status_code) == 429 or int(response.status_code) >= 500
                if not transient_status or attempt == 2:
                    break
                retry_after = response.headers.get("Retry-After", "")
                response.close()
                try:
                    delay = min(30.0, max(float(retry_after), 1.0))
                except (TypeError, ValueError):
                    delay = min(8.0, 2.0 ** attempt)
                time.sleep(delay)
            assert response is not None
            status_code = int(response.status_code)
            if status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location or redirect_count >= MAX_REDIRECTS:
                    return {"status": "redirect_limit", "http_status": status_code, "final_url": current, "html": ""}
                current = urljoin(current, location)
                continue
            if status_code != 200:
                response.close()
                return {"status": "http_error", "http_status": status_code, "final_url": current, "html": ""}
            chunks = []
            size = 0
            for chunk in response.iter_content(16_384):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    response.close()
                    return {"status": "response_too_large", "http_status": status_code, "final_url": current, "html": ""}
                chunks.append(chunk)
            body = b"".join(chunks)
            response.close()
            encoding = getattr(response, "encoding", None) or "utf-8"
            return {"status": "http_200", "http_status": status_code, "final_url": current,
                    "html": body.decode(encoding, errors="replace")}
    except requests.Timeout:
        return {"status": "timeout", "final_url": current, "html": ""}
    except requests.RequestException as exc:
        return {"status": "request_error", "reason": type(exc).__name__, "final_url": current, "html": ""}
    finally:
        if session is None:
            http.close()
    return {"status": "redirect_limit", "final_url": current, "html": ""}


def _validate_aggregator_identity(url: str, codigo: str) -> tuple[bool, str, str | None]:
    parts = urlsplit(url)
    if canonical_portal(url) != "proppit":
        return False, "not_proppit_url", None
    if parts.scheme.lower() != "https" or not _safe_host(parts.hostname or ""):
        return False, "unsafe_or_untrusted_aggregator_url", None
    query = parse_qs(parts.query, keep_blank_values=False)
    crm = (query.get("crm") or [""])[0].strip().lower()
    country = (query.get("country") or [""])[0].strip().lower()
    source_id = (query.get("id") or [""])[0].strip()
    agency = (query.get("agency") or [""])[0].strip()
    if crm != "real_time_convecta" or country != "cl" or not agency or not source_id:
        return False, "aggregator_identity_incomplete", source_id or None
    if not source_id.endswith(str(codigo).strip()):
        return False, "aggregator_property_id_mismatch", source_id
    if "sucre" not in agency.casefold() and not agency.endswith("-7"):
        return False, "aggregator_agency_mismatch", source_id
    return True, "", source_id


def parse_proppit_destinations(html: str) -> dict[str, list[str]]:
    """Collect real outbound links from aggregator HTML; never synthesize URLs."""
    result = {provider: [] for provider in PROVIDERS}
    source_text = str(html or "").replace("\\/", "/").replace("&amp;", "&")
    decoded_text = unquote(source_text)
    soup = BeautifulSoup(source_text, "html.parser")
    candidates = [a.get("href", "") for a in soup.find_all("a", href=True)]
    candidates.extend(re.findall(r"https?://[^\s\"'<>]+", source_text, flags=re.I))
    if decoded_text != source_text:
        candidates.extend(re.findall(r"https?://[^\s\"'<>]+", decoded_text, flags=re.I))
    for candidate in candidates:
        raw = str(candidate or "").strip().replace("&amp;", "&")
        portal = canonical_portal(raw)
        if portal not in result:
            continue
        parts = urlsplit(raw)
        if parts.scheme.lower() != "https" or not parts.hostname or not _safe_host(parts.hostname):
            continue
        normalized = normalize_property_url(raw)
        if normalized and all(normalize_property_url(existing) != normalized for existing in result[portal]):
            result[portal].append(raw)
    return result


def _aggregator_sources(publications: dict, existing: dict) -> list[tuple[str, str | None]]:
    proppit = publications.get("proppit") if isinstance(publications.get("proppit"), dict) else {}
    operations = proppit.get("publicaciones") if isinstance(proppit.get("publicaciones"), dict) else {}
    found = []
    for operation, entry in operations.items():
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if url and canonical_portal(url) == "proppit":
            found.append((url, _canonical_operation(operation)))
    if not found:
        old_url = str(((existing.get("publicaciones") or {}).get("proppit") or {}).get("url_proppit") or "").strip()
        if old_url and canonical_portal(old_url) == "proppit":
            found.append((old_url, None))
    deduped = []
    seen = set()
    for url, operation in found:
        key = (normalize_property_url(url), operation)
        if key not in seen:
            seen.add(key)
            deduped.append((url, operation))
    return deduped


def _add_proppit_snapshot(snapshot: dict, aggregator_url: str, operation: str | None,
                          codigo: str, checked_at: str, fetcher=safe_fetch_html) -> dict:
    valid, reason, aggregator_id = _validate_aggregator_identity(aggregator_url, codigo)
    result = {"source_status": "unverified", "reason": reason, "providers": {}}
    if not valid:
        return result
    fetched = fetcher(aggregator_url)
    if fetched.get("status") != "http_200":
        result.update(source_status=fetched.get("status") or "not_accessible",
                      reason=fetched.get("reason") or fetched.get("http_status"))
        return result
    provider_links = parse_proppit_destinations(fetched.get("html") or "")
    total = sum(len(items) for items in provider_links.values())
    result["source_status"] = "source_verified" if total else "source_verified_no_destinations"
    result["reason"] = "aggregator_listed_destinations" if total else "aggregator_has_no_supported_destinations"
    result["providers"] = {key: "source_verified" if provider_links[key] else "not_observed"
                            for key in PROVIDERS}
    proppit = snapshot.setdefault("proppit", {})
    distribution = proppit.setdefault("portales_distribucion", {})
    aliases = snapshot.setdefault("aliases", [])
    operation_value = operation or "V"
    # The aggregator link is itself a stable publication reference. Its query
    # identity is retained by normalize_property_url and the alias index.
    aggregator_alias = build_property_alias(
        aggregator_url, "proppit", operation_value, aggregator_id,
        active=True, publication_status="active", resolvable=True,
        source=PROPPIT_SOURCE, source_url=aggregator_url,
        verification_status="source_verified", first_seen_at=checked_at,
        last_seen_at=checked_at, verified_at=checked_at,
    )
    aliases.append(aggregator_alias)
    for provider, urls in provider_links.items():
        provider_node = distribution.setdefault(provider, {})
        op_node = provider_node.setdefault(operation_value, {})
        observed_urls = sorted(urls, key=lambda value: normalize_property_url(value))
        proposed_state = {
            "url_actual": observed_urls[0] if observed_urls else None,
            "external_id": extract_property_external_id(observed_urls[0], provider) if observed_urls else None,
            "verification_status": "source_verified" if urls else "not_observed",
            "destination_status": "not_checked" if urls else "not_observed",
            "source_url": aggregator_url,
            "urls_observadas": observed_urls,
        }
        changed_observation = any(op_node.get(key) != value for key, value in proposed_state.items())
        op_node.update(proposed_state)
        # The per-provider timestamp describes a changed/first observation,
        # not each identical poll; last_checked_at in the state ledger records
        # every reconciliation attempt.
        op_node["last_checked_at"] = (
            checked_at if changed_observation or not op_node.get("last_checked_at")
            else op_node.get("last_checked_at")
        )
        for url in observed_urls:
            external_id = extract_property_external_id(url, provider)
            aliases.append(build_property_alias(
                url, provider, operation_value, external_id,
                active=True, publication_status="active", resolvable=True,
                source=PROPPIT_SOURCE, source_url=aggregator_url,
                verification_status="source_verified", first_seen_at=checked_at,
                last_seen_at=checked_at, verified_at=checked_at,
            ))
    result.update(aggregator_id=aggregator_id, destination_count=total,
                  destination_urls=provider_links)
    return result


def _normalize_alias_portal(portal: str) -> set[str]:
    if portal in {"mercadolibre", "portal_inmobiliario"}:
        return {"mercadolibre", "portal_inmobiliario"}
    return {portal}


def _reference_claim_keys(reference: dict) -> set[tuple[str, str, str]]:
    portal = str(reference.get("portal") or "")
    # A publication reference is globally unique within its portal namespace.
    # A second property claiming the same MLC/URL is ambiguous even if one
    # record calls it Venta and the other Arriendo.
    operation = "*"
    keys = set()
    external_id = str(reference.get("external_id") or "").strip()
    url = str(reference.get("url_normalized") or "").strip()
    if external_id:
        mlc = re.search(r"MLC[-_ ]?(\d+)", external_id, re.I)
        ident = f"MLC-{mlc.group(1)}" if mlc else external_id.lower()
        for candidate_portal in _normalize_alias_portal(portal):
            keys.add((candidate_portal, "id:" + ident, operation))
    if url:
        for candidate_portal in _normalize_alias_portal(portal):
            keys.add((candidate_portal, "url:" + url, operation))
    return keys


def _existing_claims(docs: list[dict]) -> dict[tuple[str, str, str], set[str]]:
    claims: dict[tuple[str, str, str], set[str]] = {}
    for doc in docs:
        code = str(doc.get("codigo") or "").strip()
        publications = (doc.get("publicaciones") or {}) if isinstance(doc.get("publicaciones"), dict) else {}
        refs = collect_publication_references(publications)
        for alias in publications.get("aliases") or []:
            if not isinstance(alias, dict) or alias.get("resolvable") is False:
                continue
            status = str(alias.get("verification_status") or "").lower()
            if status in {"unverified", "not_verified", "pending", "conflict", "refuted", "not_accessible"}:
                continue
            refs.append(alias)
        for reference in refs:
            for key in _reference_claim_keys(reference):
                claims.setdefault(key, set()).add(code)
    return claims


def _incoming_conflicts(incoming: dict, own_code: str, claims: dict) -> list[dict]:
    conflicts = []
    publications = incoming.get("publicaciones") if isinstance(incoming.get("publicaciones"), dict) else incoming
    for alias in publications.get("aliases") or []:
        if not isinstance(alias, dict):
            continue
        for key in _reference_claim_keys(alias):
            other_codes = claims.get(key, set()) - {str(own_code)}
            if other_codes:
                conflicts.append({
                    "portal": alias.get("portal"),
                    "operacion": alias.get("operacion"),
                    "external_id": alias.get("external_id"),
                    "url": alias.get("url"),
                    "candidate_codes": sorted(other_codes | {str(own_code)}),
                })
    unique = {}
    for conflict in conflicts:
        unique[(conflict["portal"], conflict["operacion"], conflict["external_id"], conflict["url"])] = conflict
    return list(unique.values())


def _provider_observations(publications: dict) -> list[dict]:
    # Compatibility arrays and aliases intentionally retain old references.
    # They are useful for matching/history but must not be counted as the
    # current publication in the reconciliation diff.
    history_keys = {
        "aliases", "urls", "urls_historicas", "urls_pi", "urls_mercado_libre",
        "urls_yapo", "urls_toctoc", "urls_proppit", "urls_icasas", "urls_trovit",
        "urls_mitula", "urls_nuroa", "urls_nestoria", "urls_chilepropiedades",
    }

    def current_tree(value):
        if isinstance(value, dict):
            if value.get("publicada") is False or str(value.get("estado") or "").strip() == "0":
                return {}
            return {key: current_tree(item) for key, item in value.items()
                    if key not in history_keys}
        if isinstance(value, list):
            return [current_tree(item) for item in value]
        return value

    refs = collect_publication_references(current_tree(publications))
    return [
        {"portal": ref.get("portal"), "operacion": ref.get("operacion"),
         "url": ref.get("url"), "url_normalized": ref.get("url_normalized"),
         "external_id": ref.get("external_id")}
        for ref in refs
    ]


def _difference(old: dict, new: dict, code: str, scraped_at: str, source: str,
                status: str, error: str | None = None) -> list[dict]:
    before = _provider_observations(old)
    after = _provider_observations(new)
    keys = set()
    for row in before + after:
        keys.add((row.get("portal"), row.get("operacion")))
    output = []
    for portal, operation in sorted(keys, key=lambda x: (x[0] or "", x[1] or "")):
        old_rows = [row for row in before if row.get("portal") == portal and row.get("operacion") == operation]
        new_rows = [row for row in after if row.get("portal") == portal and row.get("operacion") == operation]
        old_urls = sorted({row.get("url") for row in old_rows if row.get("url")})
        new_urls = sorted({row.get("url") for row in new_rows if row.get("url")})
        old_ids = sorted({row.get("external_id") for row in old_rows if row.get("external_id")})
        new_ids = sorted({row.get("external_id") for row in new_rows if row.get("external_id")})
        if old_urls == new_urls and old_ids == new_ids:
            change = "unchanged"
        elif not new_urls and not new_ids:
            change = "not_found"
        elif not old_urls and not old_ids:
            change = "new"
        else:
            change = "changed"
        output.append({
            "codigo_prop360": code, "portal": portal, "operacion": operation,
            "operation": change, "old_mlc": old_ids if portal in {"mercadolibre", "portal_inmobiliario"} else [],
            "new_mlc": new_ids if portal in {"mercadolibre", "portal_inmobiliario"} else [],
            "old_external_ids": old_ids, "new_external_ids": new_ids,
            "old_urls": old_urls, "new_urls": new_urls,
            "historical_external_ids": sorted({
                str(alias.get("external_id")) for alias in (new.get("aliases") or [])
                if isinstance(alias, dict) and alias.get("portal") == portal
                and alias.get("publication_status") in {"historical", "expired", "unavailable"}
                and alias.get("external_id")
            }),
            "historical_urls": sorted({
                str(alias.get("url")) for alias in (new.get("aliases") or [])
                if isinstance(alias, dict) and alias.get("portal") == portal
                and alias.get("publication_status") in {"historical", "expired", "unavailable"}
                and alias.get("url")
            }),
            "change_type": change, "safe_to_migrate": change != "conflict",
            "reason": error or ("identity stable" if change == "unchanged" else "current source observation"),
            "scraped_at": scraped_at, "source": source, "status": status, "error": error,
        })
    return output


def _ensure_provider_rows(rows: list[dict], code: str, scraped_at: str, source: str,
                          status: str, aggregator_results: list[dict],
                          conflicts: list[dict], error: str | None = None) -> list[dict]:
    """Emit one auditable row per required Proppit destination, including absence."""
    seen = {(row.get("portal"), row.get("operacion")) for row in rows}
    aggregator_urls = [item.get("url") for item in aggregator_results if item.get("url")]
    operations = list(dict.fromkeys(item.get("operacion") for item in aggregator_results)) or [None]
    for operation in operations:
        for provider in ("proppit", *PROVIDERS):
            if (provider, operation) in seen:
                continue
            provider_results = [item for item in aggregator_results if item.get("operacion") == operation]
            urls = sorted({url for item in provider_results
                           for url in (item.get("destination_urls", {}).get(provider) or [])})
            observed = bool(urls)
            rows.append({
                "codigo_prop360": code,
                "portal": provider,
                "operacion": operation,
                "operation": "not_observed" if not observed else "new",
                "old_mlc": [], "new_mlc": [],
                "old_external_ids": [], "new_external_ids": [],
                "old_urls": [], "new_urls": urls,
                "historical_external_ids": [], "historical_urls": [],
                "change_type": "not_observed" if not observed else "new",
                "safe_to_migrate": not bool(conflicts),
                "reason": "destination_not_observed_in_proppit" if not observed else "aggregator_destination_observed",
                "scraped_at": scraped_at,
                "source": source,
                "source_url": next((item.get("url") for item in provider_results if item.get("url")), None),
                "status": "AMBIGUOUS" if conflicts else status,
                "property_status": "AMBIGUOUS" if conflicts else status,
                "error": error,
                "conflicts": conflicts,
            })
    return rows


def _pending_providers(source_meta: dict) -> list[str]:
    """Retry inaccessible aggregators and individually missing destinations."""
    pending = set()
    for item in source_meta.get("aggregator_results", []):
        if _permanent_source_block(item):
            # Never retry a source that explicitly disallows this crawler, or
            # a URL/redirect that failed the security boundary. A later normal
            # portfolio cycle can observe a policy change without retry storms.
            continue
        if item.get("source_status") != "source_verified":
            pending.update(PROVIDERS)
            continue
        for provider in PROVIDERS:
            if item.get("providers", {}).get(provider) != "source_verified":
                pending.add(provider)
    return sorted(pending)


def _permanent_source_block(item: dict) -> bool:
    status = str(item.get("source_status") or "").lower()
    reason = str(item.get("reason") or "").lower()
    if status in {"robots_blocked", "blocked", "redirect_limit", "response_too_large"}:
        return True
    return status == "http_error" and reason in {"400", "401", "403", "404", "410"}


def _blocked_source_reason(source_meta: dict) -> str | None:
    for item in source_meta.get("aggregator_results", []):
        if _permanent_source_block(item):
            return str(item.get("reason") or item.get("source_status") or "source_blocked")
    return None


def _build_incoming(client, code: str, existing_doc: dict, *, payload: dict | None = None,
                    fetcher=safe_fetch_html, checked_at: str | None = None) -> tuple[dict, dict, dict]:
    checked_at = checked_at or _iso()
    payload = payload if payload is not None else client.get_portales(code)
    if not isinstance(payload, dict):
        raise ValueError("portal_payload_not_object")
    incoming = parse_publicaciones_json(payload, code, office_id=OFFICE_ID)
    publications = deepcopy(incoming)
    aggregator_results = []
    for aggregator_url, operation in _aggregator_sources(incoming, existing_doc):
        result = _add_proppit_snapshot(publications, aggregator_url, operation, code, checked_at, fetcher=fetcher)
        aggregator_results.append({"url": aggregator_url, "operacion": operation, **result})
        if result.get("source_status") in {"source_verified", "source_verified_no_destinations"}:
            operation_key = operation or "V"
            old_distribution = (((existing_doc.get("publicaciones") or {}).get("proppit") or {})
                                .get("portales_distribucion") or {})
            new_distribution = (publications.get("proppit") or {}).get("portales_distribucion") or {}
            for provider in PROVIDERS:
                old_node = (old_distribution.get(provider) or {}).get(operation_key) or {}
                new_node = (new_distribution.get(provider) or {}).get(operation_key) or {}
                new_urls = new_node.get("urls_observadas") or []
                compared_fields = (
                    "url_actual", "external_id", "verification_status",
                    "destination_status", "source_url", "urls_observadas",
                )
                if old_node and all(old_node.get(field) == new_node.get(field) for field in compared_fields):
                    new_node["last_checked_at"] = old_node.get("last_checked_at") or checked_at
                else:
                    new_node["last_checked_at"] = checked_at
                old_url = str(old_node.get("url_actual") or "").strip()
                if old_url and not any(normalize_property_url(old_url) == normalize_property_url(url)
                                       for url in new_urls):
                    history = list(new_node.get("urls_historicas") or old_node.get("urls_historicas") or [])
                    if old_url not in history:
                        history.append(old_url)
                    new_node["urls_historicas"] = history
                    publications.setdefault("aliases", []).append(build_property_alias(
                        old_url, provider, operation_key,
                        extract_property_external_id(old_url, provider),
                        active=False, publication_status="historical", resolvable=True,
                        source=PROPPIT_SOURCE, source_url=aggregator_url,
                        verification_status="source_verified", first_seen_at=checked_at,
                        last_seen_at=checked_at, verified_at=checked_at,
                    ))
                    clear_fields = publications.setdefault("_clear_publication_fields", [])
                    for field in ("url_actual", "external_id"):
                        dotted = f"proppit.portales_distribucion.{provider}.{operation_key}.{field}"
                        if dotted not in clear_fields:
                            clear_fields.append(dotted)
    return publications, payload, {
        "aggregator_results": aggregator_results,
        "source_status": "source_verified" if payload.get("publications") is not None else "parse_error",
        "source_provider_count": len(payload.get("publications") or []),
    }


def _is_currently_active_listing(row: dict) -> bool:
    return str(row.get("estado") or "").strip().casefold() in {"activa", "active"}


def _active_docs_for_listing(coll, active_rows: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    codes = [str(row.get("codigo") or "").strip() for row in active_rows]
    query_codes: list[Any] = []
    for code in codes:
        query_codes.append(code)
        if code.isdigit():
            query_codes.append(int(code))
    docs = list(coll.find({"codigo": {"$in": query_codes}})) if query_codes else []
    grouped: dict[str, list[dict]] = {}
    for doc in docs:
        grouped.setdefault(str(doc.get("codigo") or "").strip(), []).append(doc)
    valid, errors = {}, []
    for code in codes:
        matches = grouped.get(code, [])
        if len(matches) > 1:
            errors.append({"codigo": code, "error": "mongo_duplicate_property_code"})
            continue
        doc = matches[0] if matches else None
        if doc is None:
            errors.append({"codigo": code, "error": "mongo_property_missing"})
            continue
        in_sucre, reason = _office_consistency(doc)
        if not in_sucre:
            errors.append({"codigo": code, "error": reason or "office_scope_rejected"})
            continue
        if not _active_mongo_property(doc):
            errors.append({"codigo": code, "error": "mongo_property_not_active"})
            continue
        valid[code] = doc
    return valid, errors


def _ensure_backups(docs: list[dict], run_id: str, backup_dir: str | Path | None = None) -> dict:
    from bson import json_util
    import gzip

    directory = Path(backup_dir or os.getenv("PUBLICATION_SYNC_BACKUP_DIR")
                     or (Path(__file__).resolve().parents[1] / "backups"))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"prop360_publications_sucre_pre_{run_id}.json.gz"
    payload = json_util.dumps(docs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    with gzip.open(path, "wb") as handle:
        handle.write(payload)
    with gzip.open(path, "rb") as handle:
        restored = json_util.loads(handle.read().decode("utf-8"))
    if len(restored) != len(docs):
        raise RuntimeError("backup_readback_document_count_mismatch")
    return {"path": str(path), "documents": len(restored), "sha256": digest}


def _strict_update_filter(doc: dict) -> dict:
    query = {
        "_id": doc.get("_id"),
        "codigo": doc.get("codigo"),
        "disponible_prop360": True,
    }
    for path, value in (
        ("estado.estado_prop360", (doc.get("estado") or {}).get("estado_prop360")),
        ("resumen.estado_prop360", (doc.get("resumen") or {}).get("estado_prop360")),
    ):
        query[path] = value if value is not None else {"$exists": False}
    for path, value in (
        ("oficina_id", doc.get("oficina_id")),
        ("oficina_nombre", doc.get("oficina_nombre")),
        ("resumen.oficina", (doc.get("resumen") or {}).get("oficina")),
        ("estado.oficina", (doc.get("estado") or {}).get("oficina")),
    ):
        query[path] = value if value is not None else {"$exists": False}
    # Compare-and-swap on only the publications subdocument; do not replace the
    # property document or touch unrelated CRM, price, owner, image or embedding data.
    if "publicaciones" in doc:
        query["publicaciones"] = deepcopy(doc.get("publicaciones"))
    else:
        query["publicaciones"] = {"$exists": False}
    return query


def _persist_one(coll, state_coll, doc: dict, proposed_publications: dict, run_id: str,
                 checked_at: str, *, retries: int = 3) -> tuple[bool, dict]:
    code = str(doc.get("codigo") or "").strip()
    for attempt in range(retries):
        current = coll.find_one({"_id": doc.get("_id")})
        if not current:
            return False, {"codigo": code, "error": "property_disappeared"}
        in_sucre, reason = _office_consistency(current)
        if not in_sucre or not _active_mongo_property(current):
            return False, {"codigo": code, "error": reason or "property_no_longer_active_sucre"}
        merged = merge_publication_snapshot(
            current.get("publicaciones"), proposed_publications,
            observed_at=checked_at, source="prop360_publication_sync",
        )
        if merged == (current.get("publicaciones") or {}):
            return False, {"codigo": code, "status": "unchanged"}
        update_filter = _strict_update_filter(current)
        result = coll.update_one(update_filter, {"$set": {"publicaciones": merged}})
        if getattr(result, "modified_count", 0) == 1:
            return True, {"codigo": code, "status": "updated"}
    return False, {"codigo": code, "error": "publication_compare_and_swap_conflict"}


def _retry_after(attempt_count: int, now: datetime) -> datetime:
    delays = (1, 6, 24)
    index = max(0, min(int(attempt_count) - 1, len(delays) - 1))
    return now + timedelta(hours=delays[index])


def _stable_source_hash(incoming: dict, source_meta: dict) -> str:
    payload = {
        "references": sorted(
            (str(ref.get("portal") or ""), str(ref.get("operacion") or ""),
             str(ref.get("external_id") or ""), str(ref.get("url_normalized") or ""))
            for ref in collect_publication_references(incoming)
        ),
        "aggregators": sorted(
            (item.get("url"), item.get("source_status"),
             tuple(sorted((provider, tuple(sorted(urls))) for provider, urls in (item.get("destination_urls") or {}).items())))
            for item in source_meta.get("aggregator_results", [])
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def sync_single_property_publications(client, coll, db, property_doc: dict, *,
                                      payload: dict | None = None,
                                      fetcher=safe_fetch_html,
                                      backup_dir: str | Path | None = None) -> dict:
    """Immediately reconcile a just-persisted Sucre property without touching other fields."""
    code = str(property_doc.get("codigo") or "").strip()
    in_sucre, scope_error = _office_consistency(property_doc)
    if not code or not in_sucre or not _active_mongo_property(property_doc):
        return {"codigo": code, "status": "skipped", "reason": scope_error or "property_not_active_sucre"}
    if payload is None:
        payload = getattr(client, "last_portales_by_code", {}).get(code)
    if payload is None:
        if isinstance(client, Prop360Client):
            _rate_limit(PROP360_SOURCE_HOST)
        payload = client.get_portales(code)
    checked_at = _iso()
    incoming, _payload, source_meta = _build_incoming(
        client, code, property_doc, payload=payload, fetcher=fetcher, checked_at=checked_at,
    )
    all_docs = list(coll.find({}, {"codigo": 1, "publicaciones": 1}))
    conflicts = _incoming_conflicts(incoming, code, _existing_claims(all_docs))
    if conflicts:
        state = db[STATE_COLLECTION]
        state.update_one({"_id": code}, {"$set": {
            "codigo": code, "office_id": OFFICE_ID, "last_checked_at": checked_at,
            "status": "conflict", "providers_pending": [], "error_code": "AMBIGUOUS_PROPERTY_REFERENCE",
            "run_id": uuid.uuid4().hex, "updated_count": 0, "conflicts": conflicts,
        }}, upsert=True)
        return {"codigo": code, "status": "conflict", "conflicts": conflicts, "updated": False}
    proposed = merge_publication_snapshot(
        property_doc.get("publicaciones"), incoming,
        observed_at=checked_at, source="prop360_publication_sync",
    )
    changed = proposed != (property_doc.get("publicaciones") or {})
    run_id = uuid.uuid4().hex
    backup = None
    if changed:
        backup = _ensure_backups([property_doc], run_id, backup_dir)
        if backup.get("documents") != 1:
            raise RuntimeError("single_property_backup_verification_failed")
        written, write_meta = _persist_one(coll, db[STATE_COLLECTION], property_doc,
                                           incoming, run_id, checked_at)
        changed = bool(written)
        if not written and write_meta.get("error"):
            return {"codigo": code, "status": "error", "error": write_meta["error"], "updated": False,
                    "backup": backup}
    pending = _pending_providers(source_meta)
    blocked_reason = _blocked_source_reason(source_meta)
    state_coll = db[STATE_COLLECTION]
    previous = state_coll.find_one({"_id": code}) or {}
    attempt = int(previous.get("attempt_count") or 0) + (1 if pending else 0)
    state_status = "pending" if pending else "blocked" if blocked_reason else "success"
    state_coll.update_one({"_id": code}, {"$set": {
        "codigo": code, "office_id": OFFICE_ID, "last_checked_at": checked_at,
        "last_success_at": checked_at if not pending and not blocked_reason else previous.get("last_success_at"),
        "next_retry_at": _retry_after(attempt, _utc_now()) if pending else None,
        "attempt_count": attempt if pending else 0,
        "status": state_status,
        "providers_pending": sorted(set(pending)),
        "source_hash": _stable_source_hash(incoming, source_meta),
        "run_id": run_id,
        "error_code": "proppit_pending" if pending else blocked_reason,
        "updated_count": 1 if changed else 0,
    }}, upsert=True)
    return {"codigo": code, "status": state_status,
            "updated": changed, "backup": backup,
            "aggregator_results": source_meta.get("aggregator_results", [])}


def run_publication_reconciliation(*, dry_run: bool = True, execute: bool = False,
                                   office_id: int | None = None, limit: int | None = None,
                                   codes: list[str] | None = None, db=None, prop360_client=None,
                                   client_factory=None, output_dir: str | Path | None = None,
                                   backup_dir: str | Path | None = None, fetcher=safe_fetch_html) -> dict:
    """Reconcile all active Sucre publications, defaulting to zero Mongo writes."""
    if office_id not in (None, OFFICE_ID):
        raise ValueError("publication sync is restricted to office_id=7")
    if execute and office_id != OFFICE_ID:
        raise ValueError("--execute requires explicit --office-id 7")
    if execute and dry_run:
        raise ValueError("choose exactly one of dry-run or execute")
    if execute and backup_dir is None:
        backup_dir = os.getenv("PUBLICATION_SYNC_BACKUP_DIR")
    run_id = uuid.uuid4().hex
    started = _iso()
    own_client = False
    owned_prop_client = prop360_client is None
    locked = False
    db_client = None
    result = {
        "run_id": run_id, "started_at": started, "office_id": OFFICE_ID,
        "office": OFFICE_NAME, "collection": COLLECTION_NAME,
        "dry_run": bool(dry_run), "execute": bool(execute),
        "total_active": 0, "checked": 0, "changed": 0, "unchanged": 0,
        "new_urls": 0, "historical_urls_preserved": 0, "missing_urls": 0,
        "unverified": 0, "conflicts": 0, "errors": 0,
        "mongo_active": 0, "inventory_mismatches": 0,
        "prop360_only": [], "mongo_only": [],
        "outside_office_documents_checked": 0, "outside_office_writes": 0,
        "backup": None, "records": [], "property_errors": [],
    }
    try:
        if db is None:
            db_client, coll = get_mongo_collection(COLLECTION_NAME)
            db = db_client[Config.DB_NAME]
            own_client = True
        else:
            coll = db[COLLECTION_NAME]
        if execute:
            locked = _acquire_lock(db, run_id, _utc_now(), sync_key=LOCK_KEY, office_ids=(OFFICE_ID,))
            if not locked:
                raise RuntimeError("sucre_portfolio_sync_lock_busy")
        if prop360_client is None:
            prop360_client = (client_factory or Prop360Client)(
                os.getenv("PROP360_EMAIL"), os.getenv("PROP360_PASSWORD"), delay=0.3,
            )
        if not prop360_client.login():
            raise RuntimeError("prop360_login_failed")
        rows = prop360_client.fetch_listing(OFFICE_ID)
        meta = dict(getattr(prop360_client, "last_listing_meta", {}) or {})
        valid, validation_error = _validate_listing(rows, meta, expected_office_id=OFFICE_ID)
        if not valid:
            raise RuntimeError("invalid_sucre_listing:" + str(validation_error))
        listing_active_rows = [row for row in rows if _is_currently_active_listing(row)]
        active_rows = list(listing_active_rows)
        if codes:
            requested = {str(code).strip() for code in codes}
            active_rows = [row for row in active_rows if str(row.get("codigo") or "").strip() in requested]
        if limit is not None:
            active_rows = active_rows[:max(0, int(limit))]
        result["total_active"] = len(listing_active_rows)
        result["selected_active"] = len(active_rows)
        current_docs_full, scope_errors = _active_docs_for_listing(coll, listing_active_rows)
        selected_codes = {str(item.get("codigo") or "").strip() for item in active_rows}
        current_docs = {code: doc for code, doc in current_docs_full.items() if code in selected_codes}
        result["property_errors"].extend(scope_errors)
        result["errors"] += len(scope_errors)
        all_docs = list(coll.find({}, {"codigo": 1, "oficina_id": 1, "oficina_nombre": 1,
                                     "resumen.oficina": 1, "estado.oficina": 1,
                                     "publicaciones": 1, "disponible_prop360": 1,
                                     "estado.estado_prop360": 1}))
        result["outside_office_documents_checked"] = sum(
            1 for item in all_docs if not _office_consistency(item)[0]
        )
        mongo_active_docs = [item for item in all_docs
                             if _office_consistency(item)[0] and _active_mongo_property(item)]
        mongo_active_codes = {str(item.get("codigo") or "").strip() for item in mongo_active_docs
                              if item.get("codigo") not in (None, "")}
        prop360_active_codes = {str(item.get("codigo") or "").strip() for item in listing_active_rows
                                if item.get("codigo") not in (None, "")}
        prop360_only = sorted(code for code in prop360_active_codes
                              if code not in current_docs_full)
        mongo_only = sorted(mongo_active_codes - prop360_active_codes)
        result["mongo_active"] = len(mongo_active_codes)
        result["prop360_only"] = prop360_only
        result["mongo_only"] = mongo_only
        result["inventory_mismatches"] = len(set(prop360_only) | set(mongo_only))
        result["property_errors"].extend({
            "codigo": code, "error": "inventory_mismatch",
            "reason": "active_prop360_listing_without_unique_active_sucre_mongo_document",
        } for code in prop360_only if not any(item.get("codigo") == code for item in scope_errors))
        result["property_errors"].extend({
            "codigo": code, "error": "inventory_mismatch",
            "reason": "active_sucre_mongo_document_missing_from_prop360_active_listing",
        } for code in mongo_only)
        claims = _existing_claims(all_docs)
        planned = []
        for position, row in enumerate(active_rows, start=1):
            code = str(row.get("codigo") or "").strip()
            existing = current_docs.get(code)
            if not existing:
                # Source active inventory is still reported; never create an
                # unverified Mongo property here.
                # _active_docs_for_listing already emitted the specific cause
                # (missing, inactive or office contradiction); do not count it
                # a second time here.
                continue
            checked_at = _iso()
            try:
                incoming, payload, source_meta = _build_incoming(
                    prop360_client, code, existing, fetcher=fetcher, checked_at=checked_at,
                )
                old_publications = existing.get("publicaciones") or {}
                proposed = merge_publication_snapshot(
                    old_publications, incoming, observed_at=checked_at,
                    source="prop360_portals_api",
                )
                conflicts = _incoming_conflicts(incoming, code, claims)
                if conflicts:
                    result["conflicts"] += len(conflicts)
                    # Fail closed for the affected property, without blocking
                    # independent properties in the portfolio.
                    proposed = deepcopy(old_publications)
                current_statuses = [item.get("source_status") for item in source_meta.get("aggregator_results", [])]
                status = "SUCCESS"
                if not payload.get("publications"):
                    status = "NO_PUBLICATION"
                    result["missing_urls"] += 1
                elif any(item != "source_verified" for item in current_statuses):
                    status = "PARTIAL_UNVERIFIED"
                    result["unverified"] += 1
                diffs = _difference(
                    old_publications, proposed, code, checked_at,
                    source="Prop360.get_portales+Proppit", status=status,
                    error=";".join(str(item.get("reason")) for item in source_meta.get("aggregator_results", []) if item.get("reason")) or None,
                )
                for diff in diffs:
                    diff["conflicts"] = conflicts
                    diff["safe_to_migrate"] = not conflicts
                    diff["aggregator_results"] = source_meta.get("aggregator_results", [])
                    evidence_alias = next((alias for alias in (incoming.get("aliases") or [])
                                           if isinstance(alias, dict)
                                           and alias.get("portal") == diff.get("portal")
                                           and (not diff.get("operacion")
                                                or _canonical_operation(alias.get("operacion"))
                                                == _canonical_operation(diff.get("operacion")))), None)
                    diff["evidence_level"] = (evidence_alias or {}).get("verification_status", "not_observed")
                    diff["source_url"] = (evidence_alias or {}).get("source_url")
                    diff["status"] = "AMBIGUOUS" if conflicts else status
                    diff["property_status"] = "AMBIGUOUS" if conflicts else status
                    if conflicts:
                        diff["operation"] = "conflict"
                        diff["change_type"] = "conflict"
                        diff["reason"] = "identifier_collision"
                if conflicts and not diffs:
                    result["records"].extend({
                        "codigo_prop360": code, "portal": item.get("portal"),
                        "operacion": item.get("operacion"), "operation": "conflict",
                        "old_mlc": [], "new_mlc": [], "old_urls": [], "new_urls": [],
                        "change_type": "conflict", "safe_to_migrate": False,
                        "reason": "identifier_collision", "scraped_at": checked_at,
                        "source": "Prop360.get_portales+Proppit", "status": "AMBIGUOUS",
                        "error": None, "conflicts": [item],
                    } for item in conflicts)
                property_changed = proposed != old_publications and not conflicts
                if property_changed:
                    result["changed"] += 1
                else:
                    result["unchanged"] += 1
                result["new_urls"] += sum(1 for diff in diffs if diff["change_type"] == "new")
                incoming_url_norms = {
                    normalize_property_url(alias.get("url") or "")
                    for alias in (incoming.get("aliases") or [])
                    if isinstance(alias, dict) and alias.get("url")
                }
                for old_ref in collect_publication_references(old_publications):
                    old_url = old_ref.get("url")
                    if not old_url or normalize_property_url(old_url) in incoming_url_norms:
                        continue
                    preserved = any(
                        isinstance(alias, dict)
                        and alias.get("publication_status") in {"historical", "expired", "unavailable"}
                        and (normalize_property_url(alias.get("url") or "") == normalize_property_url(old_url))
                        for alias in (proposed.get("aliases") or [])
                    )
                    if preserved:
                        result["historical_urls_preserved"] += 1
                diffs = _ensure_provider_rows(
                    diffs, code, checked_at, "Prop360.get_portales+Proppit",
                    status, source_meta.get("aggregator_results", []), conflicts,
                    error="identifier_collision" if conflicts else None,
                )
                result["records"].extend(diffs)
                planned.append({"doc": existing, "incoming": incoming, "proposed": proposed,
                                "changed": property_changed, "conflicts": conflicts,
                                "source_meta": source_meta, "checked_at": checked_at,
                                "status": status})
                result["checked"] += 1
                if position % DEFAULT_BATCH_SIZE == 0 or position == len(active_rows):
                    logger.info("Sucre publication dry-run progress: %s/%s checked",
                                min(position, result["checked"]), len(active_rows))
            except Exception as exc:
                result["errors"] += 1
                result["property_errors"].append({"codigo": code, "error": type(exc).__name__, "reason": str(exc)[:200]})

        if execute:
            changed_plans = [item for item in planned if item["changed"]]
            backup = _ensure_backups([item["doc"] for item in changed_plans], run_id, backup_dir)
            result["backup"] = backup
            if backup["documents"] != len(changed_plans):
                raise RuntimeError("backup_verification_failed")
            for item in planned:
                doc = item["doc"]
                code = str(doc.get("codigo") or "")
                state_coll = db[STATE_COLLECTION]
                if item["changed"]:
                    changed, write_meta = _persist_one(
                        coll, state_coll, doc, item["incoming"], run_id, item["checked_at"],
                    )
                    if changed:
                        result.setdefault("documents_updated", 0)
                        result["documents_updated"] += 1
                    elif write_meta.get("error"):
                        result["errors"] += 1
                        result["property_errors"].append(write_meta)
                attempt = state_coll.find_one({"_id": code}) or {}
                pending = _pending_providers(item["source_meta"])
                blocked_reason = _blocked_source_reason(item["source_meta"])
                attempt_count = int(attempt.get("attempt_count") or 0) + (1 if pending else 0)
                state_status = "pending" if pending else "blocked" if blocked_reason else item["status"].lower()
                state_doc = {
                    "codigo": code, "office_id": OFFICE_ID,
                    "last_checked_at": item["checked_at"],
                    "last_success_at": item["checked_at"] if not pending and not blocked_reason else attempt.get("last_success_at"),
                    "next_retry_at": _retry_after(attempt_count, _utc_now()) if pending else None,
                    "attempt_count": attempt_count if pending else 0,
                    "status": state_status,
                    "providers_pending": sorted(set(pending)),
                    "source_hash": _stable_source_hash(item["incoming"], item["source_meta"]),
                    "run_id": run_id, "error_code": "proppit_pending" if pending else blocked_reason,
                    "updated_count": 1 if item["changed"] else 0,
                }
                state_coll.update_one({"_id": code}, {"$set": state_doc}, upsert=True)
        else:
            # Dry-run is intentionally free of Mongo writes, including locks,
            # job-state updates and status/log collections.
            result["mongo_writes"] = 0
        output_root = Path(output_dir or (Path(__file__).resolve().parents[1] / "reports" / "publication_sync"))
        output_root.mkdir(parents=True, exist_ok=True)
        stem = f"sucre_publications_{run_id}"
        json_path = output_root / f"{stem}.json"
        csv_path = output_root / f"{stem}.csv"
        by_provider = {}
        for record in result["records"]:
            provider = str(record.get("portal") or "unknown")
            stats = by_provider.setdefault(provider, {
                "records": 0, "changed": 0, "unchanged": 0, "unverified": 0,
                "conflicts": 0, "not_found": 0, "new_urls": 0, "historical_urls": 0,
            })
            stats["records"] += 1
            change_type = record.get("change_type")
            if change_type in {"new", "changed"}:
                stats["changed"] += 1
            elif change_type == "unchanged":
                stats["unchanged"] += 1
            elif change_type == "not_found":
                stats["not_found"] += 1
            if record.get("status") in {"PARTIAL_UNVERIFIED", "NO_PUBLICATION"}:
                stats["unverified"] += 1
            if change_type == "conflict":
                stats["conflicts"] += 1
            stats["new_urls"] += len(record.get("new_urls") or [])
            stats["historical_urls"] += len(record.get("historical_urls") or [])
        result["provider_summary"] = by_provider
        result["report_json"] = str(json_path)
        result["report_csv"] = str(csv_path)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        _write_csv(csv_path, result["records"])
        return result
    finally:
        if locked:
            _release_lock(db, run_id, "completed", _utc_now(), sync_key=LOCK_KEY)
        if owned_prop_client and prop360_client is not None:
            close = getattr(getattr(prop360_client, "client", None), "close", None)
            if callable(close):
                close()
        if own_client and db_client is not None:
            db_client.close()


def _write_csv(path: Path, records: list[dict]) -> None:
    import csv

    fields = (
        "codigo_prop360", "portal", "operacion", "operation", "old_mlc", "new_mlc",
        "old_external_ids", "new_external_ids", "old_urls", "new_urls", "change_type",
        "historical_external_ids", "historical_urls", "safe_to_migrate", "reason",
        "scraped_at", "source", "source_url", "evidence_level", "status", "property_status", "error",
        "conflicts", "aggregator_results",
    )
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in records:
            writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), (list, dict))
                             else row.get(key) for key in fields})


def run_publication_sync_batch(*, dry_run: bool = True, execute: bool = False,
                               office_id: int | None = None, batch_size: int = DEFAULT_BATCH_SIZE,
                               db=None, prop360_client=None, client_factory=None,
                               backup_dir: str | Path | None = None,
                               output_dir: str | Path | None = None) -> dict:
    """Pick the next persisted 25-property window for a six-hour cron runner.

    Dry-run does not read or write the cursor/status collection. The execute
    path is intentionally explicit and reuses the same scope-locked reconciler.
    """
    if office_id != OFFICE_ID:
        raise ValueError("batch execution requires explicit --office-id 7")
    if batch_size < 1 or batch_size > DEFAULT_BATCH_SIZE:
        raise ValueError("batch_size must be between 1 and 25")
    own_client = False
    db_client = None
    if db is None:
        db_client, coll = get_mongo_collection(COLLECTION_NAME)
        db = db_client[Config.DB_NAME]
        own_client = True
    else:
        coll = db[COLLECTION_NAME]
    try:
        if dry_run:
            return run_publication_reconciliation(
                dry_run=True, office_id=OFFICE_ID, limit=batch_size, db=db,
                prop360_client=prop360_client, client_factory=client_factory,
                output_dir=output_dir,
            )
        if execute and backup_dir is None:
            backup_dir = os.getenv("PUBLICATION_SYNC_BACKUP_DIR")
        if execute and not backup_dir:
            raise ValueError("--execute requires PUBLICATION_SYNC_BACKUP_DIR or backup_dir")
        state = db[STATE_COLLECTION].find_one({"_id": "__cursor__"}) or {}
        last_code = str(state.get("last_code") or "")
        docs = [doc for doc in coll.find({"disponible_prop360": True})
                if _office_consistency(doc)[0] and _active_mongo_property(doc)]
        docs.sort(key=lambda item: str(item.get("codigo") or ""))
        now = _utc_now()
        retry_states = list(db[STATE_COLLECTION].find({
            "office_id": OFFICE_ID, "status": "pending", "next_retry_at": {"$lte": now},
        }).sort("next_retry_at", 1).limit(batch_size))
        deferred_states = list(db[STATE_COLLECTION].find({
            "office_id": OFFICE_ID, "status": "pending", "next_retry_at": {"$gt": now},
        }, {"codigo": 1, "_id": 1}))
        deferred_codes = {str(state.get("codigo") or state.get("_id") or "")
                          for state in deferred_states}
        eligible_codes = {str(doc.get("codigo") or "") for doc in docs}
        retry_codes = [str(state.get("codigo") or state.get("_id") or "")
                       for state in retry_states
                       if str(state.get("codigo") or state.get("_id") or "") in eligible_codes]
        retry_codes = list(dict.fromkeys(retry_codes))[:batch_size]
        after = [doc for doc in docs if str(doc.get("codigo") or "") > last_code
                 and str(doc.get("codigo") or "") not in retry_codes
                 and str(doc.get("codigo") or "") not in deferred_codes]
        sequential_batch = after[:max(0, batch_size - len(retry_codes))]
        if not sequential_batch and not retry_codes:
            sequential_batch = [doc for doc in docs
                                if str(doc.get("codigo") or "") not in deferred_codes][:batch_size]
        batch_codes = retry_codes + [str(doc.get("codigo") or "") for doc in sequential_batch]
        if not batch_codes:
            return {
                "run_id": uuid.uuid4().hex, "office_id": OFFICE_ID, "dry_run": False,
                "execute": True, "status": "no_due_work", "total_active": len(docs),
                "checked": 0, "changed": 0, "unchanged": 0, "conflicts": 0,
                "errors": 0, "mongo_writes": 0, "documents_updated": 0,
                "reason": "all_active_codes_are_waiting_for_their_proppit_retry_window",
            }
        batch_by_code = {str(doc.get("codigo") or ""): doc for doc in docs}
        batch = [batch_by_code[code] for code in batch_codes if code in batch_by_code]
        result = run_publication_reconciliation(
            dry_run=False, execute=True, office_id=OFFICE_ID,
                codes=[str(doc.get("codigo")) for doc in batch], db=db,
            prop360_client=prop360_client, client_factory=client_factory,
            backup_dir=backup_dir, output_dir=output_dir,
        )
        if result.get("status") != "failed":
            db[STATE_COLLECTION].update_one(
                {"_id": "__cursor__"},
                {"$set": {"office_id": OFFICE_ID,
                          "last_code": str(sequential_batch[-1].get("codigo")) if sequential_batch else last_code,
                          "updated_at": _iso(), "run_id": result.get("run_id")}},
                upsert=True,
            )
        return result
    finally:
        if own_client and db_client is not None:
            db_client.close()
