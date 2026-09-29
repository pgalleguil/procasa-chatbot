"""Canonical operation resolution for Prop360 property documents.

``tipo_operacion.tipo`` is the property type in the current master schema and
must never be interpreted as a sale/rent operation. Resolution precedence is:

1. Explicit ``tipo_operacion.venta`` / ``tipo_operacion.arriendo`` flags.
2. ``resumen.snapshot_listado.operacion`` (observed live-listing metadata).
3. Explicit operation fields on the document or its summary/state.

The operation is returned in the stable vocabulary VENTA, ARRIENDO,
VENTA_ARRIENDO, UNKNOWN. A requested operation only narrows a verified
VENTA_ARRIENDO property; it cannot override or invent an operation.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any


VENTA = "VENTA"
ARRIENDO = "ARRIENDO"
VENTA_ARRIENDO = "VENTA_ARRIENDO"
UNKNOWN = "UNKNOWN"
_SINGULAR_OPERATIONS = {VENTA, ARRIENDO}


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().casefold()
        if value in {"true", "1", "si", "sí", "yes", "activo", "activa"}:
            return True
        if value in {"false", "0", "no", "inactivo", "inactiva"}:
            return False
    return None


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _operations_in_label(value: Any) -> set[str]:
    """Parse operation labels without mistaking ``arrendada`` for arriendo."""
    text = f" {_fold(value)} "
    sale = bool(re.search(r"\bventa\b|\bvender\b|\bvendida?\b", text))
    rent = bool(re.search(r"\barriendo\b|\barrendamiento\b|\balquiler\b|\brenta\b", text))
    result: set[str] = set()
    if sale:
        result.add(VENTA)
    if rent:
        result.add(ARRIENDO)
    return result


def _result(operations: set[str]) -> str:
    if operations == {VENTA}:
        return VENTA
    if operations == {ARRIENDO}:
        return ARRIENDO
    if operations == _SINGULAR_OPERATIONS:
        return VENTA_ARRIENDO
    return UNKNOWN


def resolve_property_operation(
    doc: Mapping[str, Any] | Any,
    *,
    requested_operation: Any = None,
) -> str:
    """Resolve the supported listing operation from a master-like document.

    ``requested_operation`` is useful when a campaign row targets one side of
    a verified dual-operation property. It is ignored unless the document's
    canonical flags resolve to VENTA_ARRIENDO, and rejected on conflicts.
    """
    if isinstance(doc, str):
        resolved = _result(_operations_in_label(doc))
    elif isinstance(doc, Mapping):
        operation = doc.get("tipo_operacion")
        operation = operation if isinstance(operation, Mapping) else {}
        sale_flag = _flag(operation.get("venta"))
        rent_flag = _flag(operation.get("arriendo"))

        if sale_flag is True or rent_flag is True:
            active = set()
            if sale_flag is True:
                active.add(VENTA)
            if rent_flag is True:
                active.add(ARRIENDO)
            resolved = _result(active)
        elif sale_flag is False and rent_flag is False:
            resolved = UNKNOWN
        else:
            summary = doc.get("resumen")
            summary = summary if isinstance(summary, Mapping) else {}
            listing = summary.get("snapshot_listado")
            listing = listing if isinstance(listing, Mapping) else {}
            labels = [listing.get("operacion")]
            labels.extend((operation.get("operacion"), doc.get("operacion"), summary.get("operacion")))
            state = doc.get("estado")
            if isinstance(state, Mapping):
                labels.append(state.get("operacion"))
            resolved = UNKNOWN
            for label in labels:
                found = _operations_in_label(label)
                if found:
                    resolved = _result(found)
                    break
    else:
        resolved = UNKNOWN

    if requested_operation is not None:
        requested = _result(_operations_in_label(requested_operation))
        if requested not in _SINGULAR_OPERATIONS:
            return UNKNOWN
        if resolved == VENTA_ARRIENDO:
            return requested
        if resolved != requested:
            return UNKNOWN
    return resolved


def operation_price_block(
    doc: Mapping[str, Any],
    *,
    requested_operation: Any = None,
) -> Mapping[str, Any] | None:
    """Return only the current price block for a resolved singular operation."""
    resolved = resolve_property_operation(doc, requested_operation=requested_operation)
    if resolved not in _SINGULAR_OPERATIONS:
        return None
    operation = doc.get("tipo_operacion")
    if not isinstance(operation, Mapping):
        return None
    block = operation.get("precio_venta" if resolved == VENTA else "precio_arriendo")
    return block if isinstance(block, Mapping) else None
