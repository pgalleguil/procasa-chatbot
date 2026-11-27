"""Read-only preflight for a normalized local property-search snapshot."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

SAFE_FIELDS = {
    "listing_key", "property_code", "source_type", "source_portal", "operation",
    "property_type", "commune", "region", "sector", "neighborhood", "neighbourhood",
    "locality", "title", "description", "search_text", "canonical_url", "price_clp",
    "price_uf", "price_currency", "price_valid", "price_conversion_date",
    "price_conversion_uf_clp", "published", "operational_state", "classification_state",
    "human_veto", "last_price_verified_at", "last_availability_verified_at", "source_updated_at",
    "attributes", "features", "geo",
}
PRIVATE_KEY = re.compile(r"(?:owner|contact|email|e-mail|mail|phone|telephone|telefono|teléfono|whatsapp|conversation|lead|crm|agent_name)", re.I)
EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<!\d)(?:\+?56[\s().-]*)?(?:9[\s().-]*\d{4}[\s().-]?\d{4}|2[\s().-]*\d{4}[\s().-]?\d{4})(?!\d)")


def _check_private_keys(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            key = str(key)
            if PRIVATE_KEY.search(key):
                raise ValueError(f"campo privado no permitido en {path or 'ficha'}")
            _check_private_keys(child, f"{path}.{key}" if path else key)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _check_private_keys(child, f"{path}[{index}]")
    elif isinstance(value, str):
        leaf = path.rsplit(".", 1)[-1]
        free_text = leaf in {"title", "description", "search_text"} or ".features." in path
        url_email = leaf == "canonical_url" and EMAIL.search(value)
        if (free_text and (EMAIL.search(value) or PHONE.search(value))) or url_email:
            raise ValueError(f"texto de contacto detectado en {path or 'ficha'}")


def validate_catalog(path: Path):
    raw = path.read_bytes()
    rows = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("el snapshot debe ser un array JSON no vacío")
    keys = []
    sources, operations = Counter(), Counter()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"ficha {index + 1}: se esperaba un objeto JSON")
        unknown = set(row) - SAFE_FIELDS
        if unknown:
            raise ValueError(f"ficha {index + 1}: campos fuera de la lista pública permitida")
        _check_private_keys(row, f"ficha {index + 1}")
        key = row.get("listing_key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"ficha {index + 1}: listing_key vacío")
        keys.append(key)
        for field in ("operation", "property_type", "commune", "source_type"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"ficha {index + 1}: falta {field}")
        for field in ("price_clp", "price_uf", "price_conversion_uf_clp"):
            value = row.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError(f"ficha {index + 1}: {field} inválido")
        sources[row["source_type"]] += 1
        operations[row["operation"]] += 1
    if len(set(keys)) != len(keys):
        raise ValueError("listing_key duplicado")
    return {
        "rows": len(rows),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "sources": dict(sorted(sources.items())),
        "operations": dict(sorted(operations.items())),
        "sector_rows": sum(bool(row.get("sector") or row.get("neighborhood")
                                 or row.get("neighbourhood") or row.get("locality")) for row in rows),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    args = parser.parse_args()
    summary = validate_catalog(args.catalog)
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
