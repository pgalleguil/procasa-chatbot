"""Pure, deterministic canonical lead-origin resolution.

The resolver deliberately returns source paths and canonical labels only.  It
never exposes lead content, performs fuzzy matching, or treats a delivery
channel as a property identifier.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse

from .property_identity import normalize_identifier, normalize_portal_source


CANONICAL_ORIGINS = frozenset(
    {
        "portal_inmobiliario",
        "mercadolibre",
        "yapo",
        "toctoc",
        "proppit",
        "chilepropiedades",
        "procasa",
        "whatsapp",
        "other",
    }
)


_PRIMARY_FIELDS: tuple[str, ...] = (
    "prospecto.origen",
    "prospecto.canal_origen",
    "prospecto.plataforma",
    "prospecto.fuente_lead",
    "prospecto.portal_origen",
    "prospecto.origen_anuncio",
    "prospecto.plataforma_origen",
)
_TOP_LEVEL_FIELDS: tuple[str, ...] = ("origen", "source_type", "canal_envio")
_EVENT_FIELDS: tuple[str, ...] = (
    "source_events[].portal_source",
    "source_events[].source_system",
)
_MESSAGE_FIELDS: tuple[str, ...] = (
    "messages[].portal",
    "messages[].source",
)

_DELIVERY_ONLY_FIELDS = frozenset({"canal_envio"})
_URL_KEY_PARTS = ("url", "link", "enlace")
_URL_HOST_ORIGINS: tuple[tuple[str, str], ...] = (
    ("portalinmobiliario", "portal_inmobiliario"),
    ("mercadolibre", "mercadolibre"),
    ("yapo", "yapo"),
    ("toctoc", "toctoc"),
    ("chilepropiedades", "chilepropiedades"),
    ("proppit", "proppit"),
    ("procasa", "procasa"),
)


def _fold(value: Any) -> str:
    text = normalize_identifier(value) or ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def _label_origin(value: Any) -> str | None:
    folded = _fold(value)
    if not folded:
        return None
    compact = re.sub(r"[^a-z0-9]+", "", folded)

    direct = normalize_portal_source(value)
    if direct:
        return direct
    if compact in {"whatsapp", "whatsap"}:
        return "whatsapp"
    if compact in {"otro", "otroportal", "sitioweb", "web", "website"}:
        return "other"
    if "mlccode" in compact or "mercadolibre" in compact:
        return "mercadolibre"
    if "portalinmobiliario" in compact:
        return "portal_inmobiliario"
    if "chilepropiedades" in compact:
        return "chilepropiedades"
    if "proppit" in compact:
        return "proppit"
    if "toctoc" in compact:
        return "toctoc"
    if "yapo" in compact:
        return "yapo"
    if "procasa" in compact:
        return "procasa"
    return None


def _url_origin(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.casefold().removeprefix("www.")
    for token, origin in _URL_HOST_ORIGINS:
        if token in host:
            return origin
    return None


def _value_at_path(document: Mapping[str, Any], path: str) -> Iterable[tuple[str, Any]]:
    """Yield scalar values for a dotted path with ``[]`` list segments."""

    parts = path.split(".")

    def walk(current: Any, index: int, resolved: str) -> Iterable[tuple[str, Any]]:
        if index >= len(parts):
            yield resolved, current
            return
        part = parts[index]
        if part.endswith("[]"):
            key = part[:-2]
            value = current.get(key) if isinstance(current, Mapping) else None
            if not isinstance(value, list):
                return
            for position, item in enumerate(value):
                yield from walk(item, index + 1, f"{resolved}{key}[].")
            return
        value = current.get(part) if isinstance(current, Mapping) else None
        if value is None:
            return
        next_resolved = (
            f"{resolved}{part}"
            if not resolved or resolved.endswith(".")
            else f"{resolved}.{part}"
        )
        yield from walk(value, index + 1, next_resolved)

    yield from walk(document, 0, "")


def _iter_url_fields(value: Any, path: str) -> Iterable[tuple[str, str]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if any(part in str(key).casefold() for part in _URL_KEY_PARTS):
                if isinstance(child, str):
                    yield child_path, child
            yield from _iter_url_fields(child, child_path)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_url_fields(child, f"{path}[]")


def _evidence_sort_key(item: tuple[int, str, str, bool]) -> tuple[int, str, str]:
    rank, origin, path, _ = item
    return rank, path, origin


def resolve_lead_origin(lead: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve one lead origin using explicit fields and trusted URL domains.

    ``canal_envio`` is considered only as a fallback because the current data
    uses it primarily as a delivery route.  It therefore cannot create a
    conflict against a stronger origin field.
    """

    if not isinstance(lead, Mapping):
        return {
            "canonical_origin": None,
            "source_field": None,
            "confidence": "unknown",
            "evidence": [],
        }

    observations: list[tuple[int, str, str, bool]] = []
    for rank, path in enumerate(_PRIMARY_FIELDS):
        for resolved_path, value in _value_at_path(lead, path):
            origin = _label_origin(value)
            if origin:
                observations.append((rank, origin, resolved_path, True))

    for offset, path in enumerate(_TOP_LEVEL_FIELDS, start=len(_PRIMARY_FIELDS)):
        for resolved_path, value in _value_at_path(lead, path):
            origin = _label_origin(value)
            if origin:
                observations.append(
                    (offset, origin, resolved_path, path not in _DELIVERY_ONLY_FIELDS)
                )

    for offset, path in enumerate(_EVENT_FIELDS + _MESSAGE_FIELDS, start=20):
        for resolved_path, value in _value_at_path(lead, path):
            origin = _label_origin(value)
            if origin:
                observations.append((offset, origin, resolved_path, True))

    for path, value in _iter_url_fields(lead, ""):
        origin = _url_origin(value)
        if origin:
            observations.append((40, origin, path, True))

    if not observations:
        return {
            "canonical_origin": None,
            "source_field": None,
            "confidence": "unknown",
            "evidence": [],
        }

    reliable = [item for item in observations if item[3]]
    reliable_origins = {item[1] for item in reliable}
    evidence = [f"{path}=>{origin}" for _, origin, path, _ in sorted(observations, key=_evidence_sort_key)]
    if len(reliable_origins) > 1:
        return {
            "canonical_origin": None,
            "source_field": "CONFLICT",
            "confidence": "conflict",
            "evidence": evidence,
        }

    chosen_origin = next(iter(reliable_origins), observations[0][1])
    chosen = min(
        (item for item in observations if item[1] == chosen_origin),
        key=_evidence_sort_key,
    )
    return {
        "canonical_origin": chosen_origin if chosen_origin in CANONICAL_ORIGINS else None,
        "source_field": chosen[2],
        "confidence": "exact",
        "evidence": evidence,
    }


def origin_field_inventory(lead: Mapping[str, Any]) -> dict[str, int]:
    """Return presence counts for audit output without returning raw values."""

    inventory: dict[str, int] = {}
    for path in _PRIMARY_FIELDS + _TOP_LEVEL_FIELDS + _EVENT_FIELDS + _MESSAGE_FIELDS:
        count = sum(1 for _, value in _value_at_path(lead, path) if normalize_identifier(value))
        if count:
            inventory[path] = count
    return inventory
