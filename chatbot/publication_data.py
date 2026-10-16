"""Safe merge helpers for current and historical publication references.

The stable identity stays ``codigo``.  This module only reconciles the existing
``publicaciones`` tree and its established ``publicaciones.aliases`` index.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any

from .property_lookup import (
    build_property_alias,
    canonical_portal,
    extract_property_external_id,
    merge_property_aliases,
    normalize_property_url,
)


PROVIDER_KEYS = {
    "mercadolibre", "portal_inmobiliario", "toctoc", "yapo", "procasa",
    "proppit", "chilepropiedades", "icasas", "trovit", "mitula", "nuroa",
    "nestoria", "enlaceinmobiliario",
}
OPERATION_KEYS = {"V": "V", "A": "A", "R": "A", "venta": "V", "arriendo": "A", "rent": "A"}
URL_KEYS = {
    "url", "url_actual", "url_pi", "url_mercado_libre", "url_procasa",
    "url_toctoc", "url_yapo", "url_proppit", "url_icasas", "url_trovit",
    "url_mitula", "url_nuroa", "url_nestoria", "url_chilepropiedades",
    "url_enlaceinmobiliario",
}
URL_LIST_KEYS = {"urls", "urls_historicas", "urls_pi", "urls_mercado_libre", "urls_yapo", "urls_toctoc", "urls_proppit", "urls_icasas", "urls_trovit", "urls_mitula", "urls_nuroa", "urls_nestoria", "urls_chilepropiedades"}
HISTORICAL_URL_LIST_KEYS = {"urls_historicas"}
ID_KEYS = {"code", "code_unique", "external_id", "publication_id", "portal_id", "codigo_pi", "codigo_yapo", "codigo_venta", "codigo_arriendo"}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _operation(path: tuple[str, ...]) -> str | None:
    for item in reversed(path):
        if item in OPERATION_KEYS:
            return OPERATION_KEYS[item]
    return None


def _portal(path: tuple[str, ...], url: str = "", external_id: Any = None) -> str:
    from_url = canonical_portal(url)
    if from_url:
        return from_url
    for item in reversed(path):
        if item in PROVIDER_KEYS:
            # The legacy PI object commonly stores a Mercado Libre URL/ID.
            if item == "portal_inmobiliario" and str(external_id or "").upper().startswith("MLC"):
                return "mercadolibre"
            return item
    if str(external_id or "").upper().startswith("MLC"):
        return "mercadolibre"
    return ""


def collect_publication_references(publications: Any) -> list[dict]:
    """Extract concrete current URL/ID references from known publication fields."""
    found: dict[tuple, dict] = {}

    def add(url: Any = "", external_id: Any = None, path: tuple[str, ...] = ()) -> None:
        url_value = str(url or "").strip()
        id_value = str(external_id or "").strip()
        if not url_value and not id_value:
            return
        portal = _portal(path, url_value, id_value)
        if not portal:
            return
        operation = _operation(path)
        ext = id_value or extract_property_external_id(url_value, portal)
        # Canonicalize the shared Mercado Libre publication ID.
        if portal in {"mercadolibre", "portal_inmobiliario"} and ext:
            match = re.search(r"MLC[-_ ]?(\d+)", str(ext), re.I)
            if match:
                ext = f"MLC-{match.group(1)}"
            elif str(ext).isdigit():
                ext = f"MLC-{ext}"
        normalized = normalize_property_url(url_value) if url_value else ""
        key = (portal, operation, ext or "", normalized)
        if key not in found:
            found[key] = {
                "portal": portal,
                "operacion": operation,
                "url": url_value,
                "url_normalized": normalized,
                "external_id": ext,
            }

    def walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            # ``aliases`` are already independent persisted claims, not current
            # observations; never reinterpret them as current fields.
            local_urls = []
            local_ids = []
            for key, item in value.items():
                key_text = str(key)
                if key_text == "aliases":
                    continue
                if key_text in URL_KEYS and isinstance(item, str):
                    if item.strip():
                        local_urls.append(item)
                elif key_text in URL_LIST_KEYS and key_text not in HISTORICAL_URL_LIST_KEYS and isinstance(item, (list, tuple)):
                    for url in item:
                        if isinstance(url, str):
                            if url.strip():
                                local_urls.append(url)
                elif key_text in ID_KEYS and not isinstance(item, (dict, list, tuple)):
                    if str(item or "").strip():
                        local_ids.append(item)
            if local_urls:
                # An operation record normally carries both its provider URL
                # and source ID. Pair them so a replaced record yields one
                # complete historical alias instead of a URL-only duplicate.
                preferred_id = next((item for item in local_ids if str(item).strip()), None)
                for url in local_urls:
                    add(url, external_id=preferred_id, path=path)
            else:
                for external_id in local_ids:
                    add(external_id=external_id, path=path)
            for key, item in value.items():
                key_text = str(key)
                if key_text == "aliases" or key_text in URL_KEYS or key_text in URL_LIST_KEYS or key_text in ID_KEYS:
                    continue
                if isinstance(item, (dict, list, tuple)):
                    walk(item, path + (key_text,))
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, path)

    walk(publications if isinstance(publications, dict) else {})
    references = list(found.values())
    known_operations = {
        (item.get("portal"), item.get("external_id"), item.get("url_normalized"))
        for item in references if item.get("operacion")
    }
    # Legacy summary fields often repeat the same URL without an operation,
    # alongside the operation-specific Prop360 entry. Keep the latter as the
    # more precise identity and avoid manufacturing a second alias.
    return [item for item in references if item.get("operacion") or
            (item.get("portal"), item.get("external_id"), item.get("url_normalized")) not in known_operations]


def _deep_merge_preserving(existing: Any, incoming: Any, key: str = "") -> Any:
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = deepcopy(existing)
        for child_key, child_value in incoming.items():
            if child_key in {"aliases", "_clear_publication_fields"}:
                continue
            if child_value is None or child_value == "":
                # Partial source responses do not erase known publication data.
                continue
            merged[child_key] = _deep_merge_preserving(merged.get(child_key), child_value, child_key)
        return merged
    if key in URL_LIST_KEYS and isinstance(incoming, (list, tuple)):
        prior = existing if isinstance(existing, (list, tuple)) else []
        values = []
        for item in list(prior) + list(incoming):
            if item not in (None, "") and item not in values:
                values.append(deepcopy(item))
        return values
    if isinstance(incoming, (dict, list, tuple)):
        return deepcopy(incoming)
    return deepcopy(incoming)


def _append_replaced_current_urls(existing: Any, incoming: Any) -> None:
    """Record replaced structured ``url_actual``/``url`` in their local history."""
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return
    for key, old_value in existing.items():
        new_value = incoming.get(key)
        if key in {"url_actual", "url"} and isinstance(old_value, str) and isinstance(new_value, str):
            if old_value and new_value and normalize_property_url(old_value) != normalize_property_url(new_value):
                history = incoming.setdefault("urls_historicas", [])
                if not isinstance(history, list):
                    history = incoming["urls_historicas"] = []
                if old_value not in history:
                    history.append(old_value)
        elif isinstance(old_value, dict) and isinstance(new_value, dict):
            _append_replaced_current_urls(old_value, new_value)


def _reference_identity(reference: dict) -> tuple:
    return (
        _provider_family(str(reference.get("portal") or "")),
        str(reference.get("external_id") or ""),
        str(reference.get("url_normalized") or ""),
    )


def _provider_family(provider: str) -> str:
    return "mlc" if provider in {"mercadolibre", "portal_inmobiliario"} else provider


def _set_nested_value(target: dict, dotted_path: str, value: Any) -> None:
    parts = [part for part in str(dotted_path or "").split(".") if part]
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    if parts:
        node[parts[-1]] = value


def _as_alias(reference: dict, *, status: str, resolvable: bool = True,
              source: str, source_url: str | None, timestamp: str) -> dict:
    url = str(reference.get("url") or "")
    alias = {
        "portal": reference.get("portal"),
        "operacion": reference.get("operacion"),
        "url": url,
        "url_normalized": reference.get("url_normalized") or (normalize_property_url(url) if url else ""),
        "external_id": reference.get("external_id"),
        "activa": status == "active",
        "publication_status": status,
        "resolvable": bool(resolvable),
        "source": source,
        "first_seen_at": timestamp,
        "last_seen_at": timestamp,
        "verified_at": timestamp if source in {"prop360_portals_api", "proppit_aggregator"} else None,
        "verification_status": "source_verified" if source in {"prop360_portals_api", "proppit_aggregator"} else "legacy_database",
    }
    if source_url:
        alias["source_url"] = source_url
    return {key: value for key, value in alias.items() if value is not None}


def merge_publication_snapshot(existing: Any, incoming: Any, *, observed_at: str | None = None,
                               source: str = "prop360_portals_api") -> dict:
    """Deep-merge publication data and retain replaced current references as aliases.

    Missing/empty provider fields are treated as partial observations, not as
    deletion. Existing aliases are carried forward and deduplicated.
    """
    old = deepcopy(existing) if isinstance(existing, dict) else {}
    new = deepcopy(incoming) if isinstance(incoming, dict) else {}
    timestamp = observed_at or _utc_iso()
    clear_fields = list(new.pop("_clear_publication_fields", []) or [])
    old_refs = collect_publication_references(old)
    new_refs = collect_publication_references(new)
    new_by_provider_operation: dict[tuple[str, str | None], list[dict]] = {}
    for ref in new_refs:
        new_by_provider_operation.setdefault((_provider_family(ref["portal"]), ref.get("operacion")), []).append(ref)

    aliases = [dict(item) for item in old.get("aliases", []) if isinstance(item, dict)]
    aliases.extend(dict(item) for item in new.get("aliases", []) if isinstance(item, dict))
    for old_ref in old_refs:
        bucket = new_by_provider_operation.get((_provider_family(old_ref["portal"]), old_ref.get("operacion")))
        if bucket is None and old_ref.get("operacion") is None:
            bucket = [ref for (family, _operation), refs in new_by_provider_operation.items()
                      if family == _provider_family(old_ref["portal"]) for ref in refs]
        if not bucket:
            continue
        if any(_reference_identity(old_ref) == _reference_identity(new_ref) for new_ref in bucket):
            continue
        aliases.append(_as_alias(
            old_ref, status="historical", source="legacy_database",
            source_url=None, timestamp=timestamp,
        ))

    _append_replaced_current_urls(old, new)
    merged = _deep_merge_preserving(old, new)
    for path in clear_fields:
        _set_nested_value(merged, path, None)
    # Incoming nested data may contain aliases; the aliases list remains the
    # one canonical historical/current index.
    # Only aliases explicitly emitted by a verified source are eligible to
    # become resolvable. A printable/listing fallback can populate compatibility
    # fields, but it cannot silently become a verified publication.
    current_aliases = [
        dict(item) for item in new.get("aliases", []) if isinstance(item, dict)
    ]

    # A currently observed reference supersedes a matching historical alias,
    # while unrelated historical references are retained unchanged.
    for alias in aliases:
        if not isinstance(alias, dict):
            continue
        if alias.get("publication_status") == "active" and not any(
            _reference_identity(alias) == _reference_identity(ref) for ref in new_refs
        ):
            alias["publication_status"] = "historical"
            alias["activa"] = False
            alias["resolvable"] = alias.get("resolvable") is not False
            alias["last_seen_at"] = timestamp
    combined = merge_property_aliases(aliases, current_aliases)
    # Preserve first-seen metadata on repeated observations and update current
    # state only when the alias is new or its verification/status changed.
    merged["aliases"] = combined
    return merged


def merge_publication_subdocument(existing: Any, incoming: Any, *, observed_at: str | None = None,
                                  source: str = "prop360_portals_api") -> dict:
    """Compatibility wrapper for callers that pass the whole publications object."""
    return merge_publication_snapshot(existing, incoming, observed_at=observed_at, source=source)
