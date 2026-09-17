"""Canonical operation and geography semantics for the owner portal.

This module is deliberately small and side-effect free.  It is the shared
boundary between the Prop360 document shape and analytical consumers: no
consumer should infer an operation from the property-type label.
"""

from __future__ import annotations

import math
import unicodedata
from typing import Any, Mapping


CANONICAL_REGIONS: tuple[str, ...] = (
    "Arica y Parinacota",
    "Tarapacá",
    "Antofagasta",
    "Atacama",
    "Coquimbo",
    "Valparaíso",
    "O'Higgins",
    "Maule",
    "Ñuble",
    "Biobío",
    "La Araucanía",
    "Los Ríos",
    "Los Lagos",
    "Aysén",
    "Magallanes",
    "Región Metropolitana de Santiago",
)


def _fold(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_apostrophes = without_marks.replace("’", "'")
    return " ".join(without_apostrophes.casefold().split())


def _strict_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = _fold(value)
        if normalized in {"true", "1", "si", "sí", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _operation_label(value: Any) -> str | None:
    normalized = _fold(value)
    if normalized in {"venta", "ventas", "sale"}:
        return "venta"
    if normalized in {"arriendo", "arriendos", "alquiler", "rent"}:
        return "arriendo"
    return None


def resolve_property_operations(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve the explicit sale/rent flags from a Prop360 property.

    ``tipo_operacion.tipo`` is a property type (for example ``Casa``), not
    an operation.  A snapshot operation is retained only as a compatibility
    consistency signal and never becomes a fallback when the canonical flags
    are absent.
    """

    operation_block = doc.get("tipo_operacion")
    operation_block = operation_block if isinstance(operation_block, Mapping) else {}

    raw_sale = operation_block.get("venta")
    raw_rent = operation_block.get("arriendo")
    sale = _strict_bool(raw_sale)
    rent = _strict_bool(raw_rent)
    invalid_flags = (raw_sale is not None and sale is None) or (raw_rent is not None and rent is None)

    operations: list[str] = []
    if sale is True:
        operations.append("venta")
    if rent is True:
        operations.append("arriendo")

    snapshot = doc.get("resumen")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    listing_snapshot = snapshot.get("snapshot_listado")
    listing_snapshot = listing_snapshot if isinstance(listing_snapshot, Mapping) else {}
    secondary_operation = _operation_label(listing_snapshot.get("operacion"))

    conflict = bool(invalid_flags)
    if secondary_operation and len(operations) == 1 and secondary_operation != operations[0]:
        conflict = True
    if secondary_operation and not operations:
        conflict = True

    if invalid_flags:
        source = "invalid_operation_flags"
    elif "venta" in operation_block or "arriendo" in operation_block:
        source = "tipo_operacion.venta/arriendo"
    else:
        source = "missing_operation_flags"
    if secondary_operation:
        source = f"{source};secondary=snapshot_listado.operacion"

    primary_operation = operations[0] if len(operations) == 1 and not conflict else None
    return {
        "sale": "venta" in operations,
        "rent": "arriendo" in operations,
        "operations": operations,
        "primary_operation": primary_operation,
        "conflict": conflict,
        "source": source,
        "secondary_operation": secondary_operation,
    }


def canonical_region(value: Any) -> str | None:
    """Return an official Chilean region name or ``None`` for unknown input."""

    aliases = {
        "arica y parinacota": "Arica y Parinacota",
        "tarapaca": "Tarapacá",
        "antofagasta": "Antofagasta",
        "atacama": "Atacama",
        "coquimbo": "Coquimbo",
        "valparaiso": "Valparaíso",
        "o'higgins": "O'Higgins",
        "o higgins": "O'Higgins",
        "bernardo ohiggins": "O'Higgins",
        "bernardo o'higgins": "O'Higgins",
        "maule": "Maule",
        "nuble": "Ñuble",
        "biobio": "Biobío",
        "bio bio": "Biobío",
        "bio-bio": "Biobío",
        "araucania": "La Araucanía",
        "la araucania": "La Araucanía",
        "los rios": "Los Ríos",
        "los lagos": "Los Lagos",
        "aysen": "Aysén",
        "magallanes": "Magallanes",
        "metropolitana": "Región Metropolitana de Santiago",
        "region metropolitana": "Región Metropolitana de Santiago",
        "region metropolitana de santiago": "Región Metropolitana de Santiago",
        "rm": "Región Metropolitana de Santiago",
    }
    return aliases.get(_fold(value))


REGION_MACROZONES: dict[str, str] = {
    "Arica y Parinacota": "NORTE",
    "Tarapacá": "NORTE",
    "Antofagasta": "NORTE",
    "Atacama": "NORTE",
    "Coquimbo": "CENTRO",
    "Valparaíso": "CENTRO",
    "O'Higgins": "CENTRO",
    "Maule": "CENTRO",
    "Región Metropolitana de Santiago": "METROPOLITANA",
    "Ñuble": "SUR",
    "Biobío": "SUR",
    "La Araucanía": "SUR",
    "Los Ríos": "SUR",
    "Los Lagos": "SUR",
    "Aysén": "SUR",
    "Magallanes": "SUR",
}


def region_to_macrozone(value: Any) -> str | None:
    """Map an already canonicalizable region to its BCCh macrozone."""

    return REGION_MACROZONES.get(canonical_region(value) or "")


def operation_price(doc: Mapping[str, Any], operation: str) -> dict[str, float | None]:
    """Read only the price block belonging to one explicit operation.

    There is intentionally no cross-operation fallback: missing sale price
    does not become an arriendo price, and vice versa.
    """

    canonical_operation = _operation_label(operation)
    block = doc.get("tipo_operacion")
    block = block if isinstance(block, Mapping) else {}
    price_block = block.get(f"precio_{canonical_operation}") if canonical_operation else None
    price_block = price_block if isinstance(price_block, Mapping) else {}

    def number(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            text = str(value).strip().replace("\u00a0", " ").replace(" ", "").replace("$", "")
            if "," in text and "." in text:
                text = text.replace(".", "").replace(",", ".")
            elif "," in text:
                text = text.replace(",", ".")
            result = float(text)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) and result > 0 else None

    return {"uf": number(price_block.get("precio_uf")), "clp": number(price_block.get("precio_clp"))}


__all__ = [
    "CANONICAL_REGIONS",
    "REGION_MACROZONES",
    "canonical_region",
    "operation_price",
    "region_to_macrozone",
    "resolve_property_operations",
]
