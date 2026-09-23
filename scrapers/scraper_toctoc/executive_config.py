"""Resolve reusable TOCTOC run configuration from CRM and existing defaults.

The resolver deliberately keeps provenance for every field. It does not infer
commercial limits from a particular executive's past assignments.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any


CONFIG_KEYS = {
    "communes": ("communes", "comunas", "communes_norm", "comunas_interes_norm"),
    "operations": ("operations", "operation", "operaciones", "operacion"),
    "property_types": ("property_types", "property_type", "tipos_propiedad", "tipo_propiedad", "tipo"),
    "min_price_clp": ("min_price_clp", "precio_minimo_clp", "min_price"),
    "max_price_clp": ("max_price_clp", "precio_maximo_clp", "max_price"),
}

NESTED_OVERRIDE_FIELDS = (
    "toctoc_scraping_config",
    "captacion_scraping_config",
    "scraping_config",
)

# These defaults come from the existing team launcher and TOCTOC runner:
# scripts/run_toctoc_team_preferences.py defaults to venta,arriendo;
# run_toctoc.py defaults to departamento and min_price_clp=0, with no max.
SYSTEM_DEFAULTS = {
    "operations": ["venta", "arriendo"],
    "property_types": ["departamento"],
    "min_price_clp": 0,
    "max_price_clp": None,
}

SYSTEM_DEFAULT_SOURCES = {
    "operations": "scripts/run_toctoc_team_preferences.py:--operations default",
    "property_types": "scrapers/scraper_toctoc/run_toctoc.py:--tipo default",
    "min_price_clp": "scrapers/scraper_toctoc/run_toctoc.py:--min-price-clp default",
    "max_price_clp": "scrapers/scraper_toctoc/run_toctoc.py:no maximum-price gate",
}


def _normal(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _first_present(mapping: dict[str, Any], aliases: tuple[str, ...]) -> tuple[bool, Any]:
    for key in aliases:
        if key in mapping:
            return True, mapping[key]
    return False, None


def _find_executive(db: Any, executive: str | dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(executive, dict):
        return dict(executive)
    target = str(executive or "").strip()
    if not target:
        return None
    users = db["usuarios"]
    # Resolve by exact id first without requiring bson in unit-test fakes.
    try:
        from bson import ObjectId
        if ObjectId.is_valid(target):
            row = users.find_one({"_id": ObjectId(target)})
            if row:
                return dict(row)
    except Exception:
        pass
    target_norm = _normal(target)
    rows = users.find(
        {"is_active": True, "rol": "agente"},
        {
            "nombre": 1,
            "_id": 1,
            "is_active": 1,
            "rol": 1,
            "comunas_interes_norm": 1,
            "comunas_interes": 1,
            "oficina": 1,
            "office": 1,
            **{key: 1 for key in NESTED_OVERRIDE_FIELDS},
        },
    )
    matches = [dict(row) for row in rows if _normal(row.get("nombre")) == target_norm]
    return matches[0] if len(matches) == 1 else None


def _find_team_default(db: Any, profile: dict[str, Any]) -> dict[str, Any]:
    """Read an optional team/office override, if one is already configured."""
    try:
        collections = set(db.list_collection_names())
    except Exception:
        return {}
    member_id = profile.get("_id")
    team_ids: list[Any] = []
    if "captacion_team_memberships" in collections:
        memberships = list(db["captacion_team_memberships"].find({}))
        for row in memberships:
            member_values = (row.get("user_id"), row.get("executive_id"), row.get("usuario_id"))
            if any(str(value) == str(member_id) for value in member_values if value is not None):
                team_id = row.get("team_id") or row.get("office_id")
                if team_id is not None:
                    team_ids.append(team_id)
    for collection_name in ("toctoc_scraping_defaults", "captacion_scraping_defaults"):
        if collection_name not in collections:
            continue
        rows = list(db[collection_name].find({}))
        for row in rows:
            row_team = row.get("team_id") or row.get("office_id")
            row_office = row.get("office") or row.get("oficina")
            if (row_team is not None and any(str(row_team) == str(team_id) for team_id in team_ids)) or (
                row_office and _normal(row_office) in {
                    _normal(profile.get("oficina")), _normal(profile.get("office"))
                }
            ):
                config = row.get("toctoc_scraping_config") or row.get("scraping_config") or row
                return dict(config) if isinstance(config, dict) else {}
    return {}


def resolve_scraping_config(
    db: Any,
    executive: str | dict[str, Any],
    *,
    system_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve executive override → team/office default → current system default.

    Missing communes are never synthesized. All returned values include a
    per-field source so the caller can distinguish CRM preferences from code
    defaults and can refuse writes when a required field is unresolved.
    """
    profile = _find_executive(db, executive)
    if not profile:
        return {
            "executive": str(executive),
            "found": False,
            "config_complete": False,
            "missing_fields": ["communes", "operations", "property_types", "min_price_clp", "max_price_clp"],
            "values": {},
            "sources": {},
        }

    executive_config: dict[str, Any] = {}
    for key in NESTED_OVERRIDE_FIELDS:
        if isinstance(profile.get(key), dict):
            executive_config.update(profile[key])
    team_config = _find_team_default(db, profile)
    defaults = dict(SYSTEM_DEFAULTS if system_defaults is None else system_defaults)
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}

    for field, aliases in CONFIG_KEYS.items():
        present, value = _first_present(executive_config, aliases)
        if present:
            source = "EXECUTIVE_OVERRIDE"
        else:
            present, value = _first_present(team_config, aliases)
            if present:
                source = "TEAM_OFFICE_DEFAULT"
            elif field == "communes":
                present, value = _first_present(profile, ("comunas_interes_norm", "comunas_interes"))
                source = "CRM_EXECUTIVE_PREFERENCES" if present else "CONFIG_MISSING"
            elif field in defaults:
                present, value = True, defaults[field]
                source = SYSTEM_DEFAULT_SOURCES.get(field, "SYSTEM_DEFAULT")
            else:
                source = "CONFIG_MISSING"
        if not present:
            values[field] = None
            sources[field] = "CONFIG_MISSING"
            continue
        if field in {"communes", "operations", "property_types"}:
            items = [str(item).strip() for item in _as_list(value) if str(item).strip()]
            values[field] = list(dict.fromkeys(items))
        elif field in {"min_price_clp", "max_price_clp"}:
            values[field] = None if value is None else int(value)
        sources[field] = source

    missing = [
        field for field, value in values.items()
        if field != "max_price_clp" and (value is None or value == [])
    ]
    # An explicitly sourced None means “no maximum”; only an absent source is
    # incomplete.
    if sources.get("max_price_clp") == "CONFIG_MISSING":
        missing.append("max_price_clp")
    return {
        "executive": profile.get("nombre") or str(executive),
        "executive_id": str(profile.get("_id") or ""),
        "found": True,
        "config_complete": not missing,
        "missing_fields": sorted(set(missing)),
        "values": values,
        "sources": sources,
        "profile": profile,
    }


__all__ = ["resolve_scraping_config", "SYSTEM_DEFAULTS", "SYSTEM_DEFAULT_SOURCES"]
