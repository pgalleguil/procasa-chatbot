"""Read-only audit of operation and region semantics in the SUCRE portfolio.

The script intentionally reads only ``universo_cartera_prop360``.  It never
writes MongoDB and fails closed when the Mongo connection is not configured.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from pymongo import MongoClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from owner_portal.semantics import (  # noqa: E402
    canonical_region,
    operation_price,
    resolve_property_operations,
)


OFFICE_VALUE = "PROCASA SUCRE"
COLLECTION_NAME = "universo_cartera_prop360"
PROJECTION = {
    "_id": 0,
    "codigo": 1,
    "estado.oficina": 1,
    "estado.disponible_prop360": 1,
    "tipo_operacion": 1,
    "resumen.snapshot_listado.operacion": 1,
    "metadata.tipo_propiedad": 1,
    "ubicacion.region": 1,
}


def _value(document: dict[str, Any], path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def audit_documents(documents: list[dict[str, Any]]) -> dict[str, Any]:
    active = [
        document
        for document in documents
        if _value(document, "estado.oficina") == OFFICE_VALUE
        and _value(document, "estado.disponible_prop360") is True
    ]
    sale_only = rent_only = both = no_operation = 0
    sale_price_missing = rent_price_missing = 0
    raw_regions: Counter[str] = Counter()
    canonical_regions: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()
    property_types_by_operation: dict[str, Counter[str]] = defaultdict(Counter)
    conflicts: list[str] = []

    for document in active:
        code = str(document.get("codigo") or "<sin-codigo>")
        resolved = resolve_property_operations(document)
        operations = tuple(resolved["operations"])
        if operations == ("venta",):
            sale_only += 1
        elif operations == ("arriendo",):
            rent_only += 1
        elif set(operations) == {"venta", "arriendo"}:
            both += 1
        else:
            no_operation += 1
        if resolved["conflict"]:
            conflicts.append(code)

        if "venta" in operations:
            if not any(operation_price(document, "venta").values()):
                sale_price_missing += 1
            property_types_by_operation["venta"][str(_value(document, "metadata.tipo_propiedad") or "<sin-tipo>")] += 1
        if "arriendo" in operations:
            if not any(operation_price(document, "arriendo").values()):
                rent_price_missing += 1
            property_types_by_operation["arriendo"][str(_value(document, "metadata.tipo_propiedad") or "<sin-tipo>")] += 1

        raw_region = str(_value(document, "ubicacion.region") or "<sin-region>")
        raw_regions[raw_region] += 1
        resolved_region = canonical_region(_value(document, "ubicacion.region"))
        if resolved_region:
            canonical_regions[resolved_region] += 1
        else:
            unresolved[raw_region] += 1

    return {
        "active_total": len(active),
        "sale_only": sale_only,
        "rent_only": rent_only,
        "both": both,
        "no_operation": no_operation,
        "sale_price_missing": sale_price_missing,
        "rent_price_missing": rent_price_missing,
        "regions_raw": dict(sorted(raw_regions.items())),
        "regions_canonical": dict(sorted(canonical_regions.items())),
        "regions_unresolved": dict(sorted(unresolved.items())),
        "property_types_by_operation": {
            operation: dict(sorted(counts.items()))
            for operation, counts in sorted(property_types_by_operation.items())
        },
        "operation_conflicts": sorted(conflicts),
    }


def main() -> int:
    mongo_uri = os.getenv("MONGO_URI") or getattr(Config, "MONGO_URI", None)
    if not mongo_uri:
        print(
            "MONGO_URI no está configurado; auditoría cancelada sin conexión ni escrituras.",
            file=sys.stderr,
        )
        return 2

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000)
    try:
        client.admin.command("ping")
        database = client[getattr(Config, "DB_NAME", "URLS")]
        documents = list(
            database[COLLECTION_NAME].find(
                {"estado.oficina": OFFICE_VALUE, "estado.disponible_prop360": True},
                PROJECTION,
            )
        )
        print(json.dumps(audit_documents(documents), ensure_ascii=False, indent=2))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
