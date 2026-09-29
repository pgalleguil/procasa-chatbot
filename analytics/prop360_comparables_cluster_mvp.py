"""MVP productivo de clustering y comparables para la cartera Prop360.

El módulo es deliberadamente autónomo: lee únicamente las dos colecciones
fuente y escribe exclusivamente en ``prop360_comparables_cluster``.  La
normalización se hace en memoria para conservar trazabilidad y evitar cambios
en ``universo_cartera_prop360`` o ``propiedades_captacion``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from pymongo import MongoClient, UpdateOne
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from scripts.property_operation import operation_price_block, resolve_property_operation  # noqa: E402


MODEL_VERSION = "cluster_mvp_20260921_v1"
OUTPUT_COLLECTION = "prop360_comparables_cluster"
PORTFOLIO_COLLECTION = "universo_cartera_prop360"
MARKET_COLLECTION = "propiedades_captacion"
OFFICE = "PROCASA SUCRE"

MIN_SURFACE_M2 = 5.0
MAX_BUILT_M2 = 5_000.0
MAX_LAND_M2 = 10_000_000.0
MIN_SALE_UF = 50.0
MAX_SALE_UF = 1_000_000.0
MIN_RENT_CLP = 10_000.0
MAX_RENT_CLP = 100_000_000.0
SILHOUETTE_SAMPLE_SIZE = 1_500

SUPPORTED_TYPES = {
    "casa",
    "departamento",
    "parcela",
    "sitio",
    "oficina",
    "local_comercial",
    "bodega",
    "industrial",
    "estacionamiento",
}
SIMPLE_SURFACE_TYPES = {"oficina", "local_comercial", "bodega", "industrial", "estacionamiento"}
_K_CACHE: dict[tuple[tuple[str, ...], tuple[str, ...]], dict[str, Any] | None] = {}

# Se usa comuna_slug como llave.  El fallback regional sólo se aplica cuando
# la comuna no está en este catálogo; nunca reemplaza la llave geográfica.
CANONICAL_REGION_BY_COMUNA = {
    # Arica y Parinacota / Tarapacá / Antofagasta
    "arica": "Arica y Parinacota",
    "alto-hospicio": "Tarapacá",
    "iquique": "Tarapacá",
    "antofagasta": "Antofagasta",
    "calama": "Antofagasta",
    "san-pedro-de-atacama": "Antofagasta",
    # Atacama / Coquimbo
    "caldera": "Atacama",
    "copiapo": "Atacama",
    "huasco": "Atacama",
    "coquimbo": "Coquimbo",
    "la-serena": "Coquimbo",
    "ovalle": "Coquimbo",
    # Valparaíso
    "algarrobo": "Valparaíso",
    "casablanca": "Valparaíso",
    "el-tabo": "Valparaíso",
    "los-andes": "Valparaíso",
    "puchuncavi": "Valparaíso",
    "quillota": "Valparaíso",
    "quilpue": "Valparaíso",
    "san-felipe": "Valparaíso",
    "valparaiso": "Valparaíso",
    "villa-alemana": "Valparaíso",
    "vina-del-mar": "Valparaíso",
    "zapallar": "Valparaíso",
    # Metropolitana
    "buin": "Región Metropolitana",
    "calera-de-tango": "Región Metropolitana",
    "cerrillos": "Región Metropolitana",
    "cerro-navia": "Región Metropolitana",
    "colina": "Región Metropolitana",
    "conchali": "Región Metropolitana",
    "curacavi": "Región Metropolitana",
    "el-bosque": "Región Metropolitana",
    "el-monte": "Región Metropolitana",
    "estacion-central": "Región Metropolitana",
    "huechuraba": "Región Metropolitana",
    "isla-de-maipo": "Región Metropolitana",
    "la-cisterna": "Región Metropolitana",
    "la-florida": "Región Metropolitana",
    "la-granja": "Región Metropolitana",
    "la-pintana": "Región Metropolitana",
    "la-reina": "Región Metropolitana",
    "lampa": "Región Metropolitana",
    "las-condes": "Región Metropolitana",
    "lo-barnechea": "Región Metropolitana",
    "lo-espejo": "Región Metropolitana",
    "lo-prado": "Región Metropolitana",
    "macul": "Región Metropolitana",
    "maipu": "Región Metropolitana",
    "malloco": "Región Metropolitana",
    "melipilla": "Región Metropolitana",
    "nunoa": "Región Metropolitana",
    "padre-hurtado": "Región Metropolitana",
    "penaflor": "Región Metropolitana",
    "penalolen": "Región Metropolitana",
    "pirque": "Región Metropolitana",
    "providencia": "Región Metropolitana",
    "pudahuel": "Región Metropolitana",
    "puente-alto": "Región Metropolitana",
    "quilicura": "Región Metropolitana",
    "quinta-normal": "Región Metropolitana",
    "recoleta": "Región Metropolitana",
    "region-metropolitana": "Región Metropolitana",
    "renca": "Región Metropolitana",
    "san-bernardo": "Región Metropolitana",
    "san-joaquin": "Región Metropolitana",
    "san-jose-de-maipo": "Región Metropolitana",
    "san-miguel": "Región Metropolitana",
    "san-ramon": "Región Metropolitana",
    "santiago": "Región Metropolitana",
    "talagante": "Región Metropolitana",
    "til-til": "Región Metropolitana",
    "vitacura": "Región Metropolitana",
    # O'Higgins / Maule / Ñuble / Biobío
    "chepica": "O'Higgins",
    "donihue": "O'Higgins",
    "machali": "O'Higgins",
    "malloa": "O'Higgins",
    "marchigue": "O'Higgins",
    "navidad": "O'Higgins",
    "paredones": "O'Higgins",
    "rancagua": "O'Higgins",
    "rengo": "O'Higgins",
    "requinoa": "O'Higgins",
    "san-fernando": "O'Higgins",
    "san-francisco-de-mostazal": "O'Higgins",
    "san-vicente": "O'Higgins",
    "santa-cruz": "O'Higgins",
    "colbun": "Maule",
    "curico": "Maule",
    "linares": "Maule",
    "longavi": "Maule",
    "maule": "Maule",
    "molina": "Maule",
    "parral": "Maule",
    "pelarco": "Maule",
    "rio-claro": "Maule",
    "san-clemente": "Maule",
    "san-javier": "Maule",
    "san-rafael": "Maule",
    "talca": "Maule",
    "villa-alegre": "Maule",
    "yerbas-buenas": "Maule",
    "chillan": "Ñuble",
    "chillan-viejo": "Ñuble",
    "quillon": "Ñuble",
    "san-carlos": "Ñuble",
    "canete": "Biobío",
    "chiguayante": "Biobío",
    "concepcion": "Biobío",
    "los-angeles": "Biobío",
    "penco": "Biobío",
    "san-pedro-de-la-paz": "Biobío",
    "tome": "Biobío",
    # Araucanía / Los Ríos / Los Lagos / Aysén / Magallanes
    "nueva-imperial": "Araucanía",
    "padre-las-casas": "Araucanía",
    "pitrufquen": "Araucanía",
    "pucon": "Araucanía",
    "temuco": "Araucanía",
    "traiguen": "Araucanía",
    "futrono": "Los Ríos",
    "mafil": "Los Ríos",
    "valdivia": "Los Ríos",
    "osorno": "Los Lagos",
    "puerto-montt": "Los Lagos",
    "puerto-varas": "Los Lagos",
    "coyhaique": "Aysén",
    "punta-arenas": "Magallanes y de la Antártica Chilena",
}

REGION_ALIASES = {
    "metropolitana": "Región Metropolitana",
    "metropolitana de santiago": "Región Metropolitana",
    "region metropolitana de santiago (rm)": "Región Metropolitana",
    "valparaiso": "Valparaíso",
    "v region de valparaiso": "Valparaíso",
    "maule": "Maule",
    "vii region del maule": "Maule",
    "biobio": "Biobío",
    "bio bio": "Biobío",
    "bío-bío": "Biobío",
    "araucania": "Araucanía",
    "coquimbo": "Coquimbo",
    "antofagasta": "Antofagasta",
    "los rios": "Los Ríos",
    "los lagos": "Los Lagos",
    "nuble": "Ñuble",
    "xvi region de nuble": "Ñuble",
    "vi region del libertador general bernardo o'higgins": "O'Higgins",
}


def _repair_common_replacement_chars(value: Any) -> str:
    text = str(value or "")
    replacements = {
        "chill�n": "chillan",
        "Chill�n": "Chillan",
        "�uble": "Nuble",
        "�u�oa": "Nunoa",
        "Vi�a del Mar": "Vina del Mar",
        "Estaci�n Central": "Estacion Central",
        "B�o-B�o": "Bio-Bio",
        "Regi�n": "Region",
        "Araucan�a": "Araucania",
        "Valpara�so": "Valparaiso",
        "Los R�os": "Los Rios",
        "�rea": "Area",
        "m�": "m2",
        "m²": "m2",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def slugify(value: Any) -> str:
    text = _repair_common_replacement_chars(value).strip().casefold()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def canonical_region(comuna_slug: str, raw_region: Any) -> str:
    if comuna_slug in CANONICAL_REGION_BY_COMUNA:
        return CANONICAL_REGION_BY_COMUNA[comuna_slug]
    raw = slugify(raw_region).replace("-", " ")
    return REGION_ALIASES.get(raw, _repair_common_replacement_chars(raw_region).strip() or "N/A")


def normalize_operation(value: Any, *, venta: Any = None, arriendo: Any = None) -> str:
    if venta is True and arriendo is True:
        return "venta_arriendo"
    if venta is True:
        return "venta"
    if arriendo is True:
        return "arriendo"
    text = slugify(value)
    if "arriend" in text or "alquiler" in text:
        return "arriendo"
    if "venta" in text:
        return "venta"
    return "N/A"


def normalize_type(value: Any) -> str:
    text = slugify(value)
    if text in {"casa", "casas"} or text.startswith("casa-"):
        return "casa"
    if text in {"departamento", "departamentos", "depto", "dpto"}:
        return "departamento"
    if text in {"parcela", "parcelas"}:
        return "parcela"
    if text in {"sitio", "sitios", "terreno", "terrenos"}:
        return "sitio"
    if "local" in text and "bodega" in text:
        return "local_comercial"
    if text in {"local", "local-comercial", "locales-comerciales", "comercial"}:
        return "local_comercial"
    if "oficina" in text:
        return "oficina"
    if "bodega" in text:
        return "bodega"
    if "industrial" in text:
        return "industrial"
    if "estacionamiento" in text:
        return "estacionamiento"
    return "N/A"


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, np.number)):
        result = float(value)
        return result if math.isfinite(result) else None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"[-+]?\d[\d\s.,]*", text)
    if not match:
        return None
    token = match.group(0).replace(" ", "")
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        tail = token.rsplit(",", 1)[1]
        token = token.replace(",", ".") if len(tail) != 3 else token.replace(",", "")
    elif token.count(".") > 1:
        token = token.replace(".", "")
    elif "." in token and len(token.rsplit(".", 1)[1]) == 3:
        token = token.replace(".", "")
    try:
        result = float(token)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def valid_surface(value: Any, *, land: bool = False) -> float | None:
    number = to_float(value)
    upper = MAX_LAND_M2 if land else MAX_BUILT_M2
    if number is None or not (MIN_SURFACE_M2 <= number <= upper):
        return None
    return number


def valid_count(value: Any, maximum: float = 20.0) -> float | None:
    number = to_float(value)
    if number is None or not (0 <= number <= maximum):
        return None
    return number


def extract_area_from_text(text: Any, *, land: bool) -> float | None:
    """Fallback conservador para publicaciones que no llenaron m2_*.

    Sólo acepta un número inmediatamente asociado a m2 y a una etiqueta de
    superficie. No intenta inferir superficies desde números sueltos.
    """
    source = _repair_common_replacement_chars(text).casefold()
    if not source:
        return None
    if land:
        labels = r"(?:terreno|sitio|parcela|superficie\s+total|area\s+total|área\s+total)"
    else:
        labels = r"(?:construid[ao]s?|util|útil|construccion|construcción|area\s+construida|área\s+construida)"
    patterns = (
        rf"{labels}[^\d]{{0,40}}(\d[\d\s.,]*)\s*m\s*(?:2|²)",
        rf"(\d[\d\s.,]*)\s*m\s*(?:2|²)[^a-z]{{0,15}}{labels}",
    )
    for pattern in patterns:
        match = re.search(pattern, source, flags=re.IGNORECASE)
        if match:
            result = valid_surface(match.group(1), land=land)
            if result is not None:
                return result
    return None


def uf_value_for(doc: dict[str, Any]) -> float:
    for key in ("uf_valor_usado", "uf_value"):
        candidate = to_float(doc.get(key))
        if candidate and 20_000 <= candidate <= 70_000:
            return candidate
    configured = float(getattr(Config, "UF_VALUE", 0) or 0)
    return configured if 20_000 <= configured <= 70_000 else 40_000.0


def normalize_price(doc: dict[str, Any], operation: str) -> dict[str, Any]:
    uf = to_float(doc.get("precio_uf_normalizado")) or to_float(doc.get("precio_uf"))
    clp = to_float(doc.get("precio_clp_normalizado")) or to_float(doc.get("precio_clp"))
    uf_value = uf_value_for(doc)
    reasons: list[str] = []
    if uf is not None and not (MIN_SALE_UF <= uf <= MAX_SALE_UF):
        uf = None
        reasons.append("uf_fuera_de_rango")
        if clp is not None:
            reasons.append("precio_uf_clp_inconsistente")
    if clp is not None and not (MIN_RENT_CLP <= clp <= 2_000_000_000):
        clp = None
        reasons.append("clp_fuera_de_rango")

    if uf is not None and clp is not None:
        implied = clp / uf if uf else 0
        if not (20_000 <= implied <= 70_000):
            reasons.append("precio_uf_clp_inconsistente")
            uf = clp / uf_value if clp else uf
        else:
            uf = clp / implied if implied else uf

    if operation == "venta":
        if uf is None and clp is not None:
            uf = clp / uf_value
        if uf is None or not (MIN_SALE_UF <= uf <= MAX_SALE_UF):
            return {"valid": False, "reason": reasons + ["precio_venta_faltante_o_invalido"]}
        if clp is None:
            clp = uf * uf_value
        return {"valid": True, "price_uf": round(uf, 4), "price_clp": round(clp, 2), "reasons": reasons}

    if operation == "arriendo":
        if clp is None and uf is not None:
            clp = uf * uf_value
        if clp is None or not (MIN_RENT_CLP <= clp <= MAX_RENT_CLP):
            return {"valid": False, "reason": reasons + ["precio_arriendo_faltante_o_invalido"]}
        return {"valid": True, "price_clp": round(clp, 2), "price_uf": round(clp / uf_value, 4), "reasons": reasons}

    return {"valid": False, "reason": ["operacion_no_soportada"]}


def normalize_portfolio(doc: dict[str, Any]) -> dict[str, Any]:
    op = doc.get("tipo_operacion") or {}
    location = doc.get("ubicacion") or {}
    car = doc.get("caracteristicas") or {}
    type_name = (doc.get("metadata") or {}).get("tipo_propiedad") or op.get("tipo")
    comuna = location.get("comuna") or ""
    comuna_slug = slugify(comuna)
    resolved_operation = resolve_property_operation(doc)
    operation = {
        "VENTA": "venta",
        "ARRIENDO": "arriendo",
        # Campaign comparables follow the approved sale-first rule for dual listings.
        "VENTA_ARRIENDO": "venta",
        "UNKNOWN": "unknown",
    }[resolved_operation]
    current_price = operation_price_block(
        doc,
        requested_operation="VENTA" if resolved_operation == "VENTA_ARRIENDO" else None,
    )
    price_doc = dict(current_price) if current_price is not None else {}
    if current_price is not None:
        price_doc["uf_valor_usado"] = current_price.get("uf_valor_conversion")
    built = valid_surface(car.get("superficie_construida"))
    useful = valid_surface(car.get("superficie_util"))
    total = valid_surface(car.get("superficie_total"))
    land = valid_surface(car.get("superficie_terreno"), land=True)
    tipo_normalizado = normalize_type(type_name)
    if tipo_normalizado == "departamento":
        # Comparable department area is the built area when explicitly
        # available; useful area is the sole fallback. Total area is not a
        # substitute for either measurement.
        surface_ref = built or useful
    elif tipo_normalizado in SIMPLE_SURFACE_TYPES:
        surface_ref = built or useful or total or land
    else:
        surface_ref = None
    return {
        "codigo": str(doc.get("codigo")),
        "office": OFFICE,
        "operation": operation,
        "tipo_normalizado": tipo_normalizado,
        "tipo_original": type_name,
        "comuna": _repair_common_replacement_chars(comuna).strip() or "N/A",
        "comuna_slug": comuna_slug or "N/A",
        "region": canonical_region(comuna_slug, location.get("region")),
        "price": normalize_price(price_doc, operation),
        "built_m2": built,
        "useful_m2": useful,
        "total_m2": total,
        "land_m2": land,
        "surface_ref_m2": surface_ref,
        "bedrooms": valid_count(car.get("dormitorios")),
        "bathrooms": valid_count(car.get("banos")),
        "parking": valid_count(car.get("estacionamientos")),
    }


def _surface_source_maps(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return all source maps used by the single auditable surface extractor."""
    sources: list[tuple[str, dict[str, Any]]] = [("root", doc)]
    for field in ("attributes", "raw_attributes"):
        value = doc.get(field)
        if isinstance(value, dict):
            sources.append((field, value))
    return sources


def _mapping_surface_candidate(mapping: dict[str, Any], kind: str, tipo: str) -> tuple[float | None, bool, str | None]:
    """Read portal attribute maps without confusing price/m² with area."""
    for raw_key, raw_value in mapping.items():
        key = slugify(raw_key)
        if not key or "precio" in key:
            continue
        is_built = (
            ("constru" in key or "construccion" in key or "util" in key)
            and any(token in key for token in ("area", "superficie", "m2", "metro"))
        )
        is_explicit_land = any(token in key for token in ("terreno", "parcela", "sitio"))
        is_total_land = any(token in key for token in ("totales", "total", "area-total", "superficie-total"))
        if kind == "built" and is_built:
            return valid_surface(raw_value), True, str(raw_key)
        if kind == "land" and tipo != "departamento" and (is_explicit_land or is_total_land):
            return valid_surface(raw_value, land=True), True, str(raw_key)
    return None, False, None


def extract_market_surface(doc: dict[str, Any], kind: str, tipo: str) -> dict[str, Any]:
    """Extract built/land area with source and raw-field audit metadata.

    Built and land are intentionally separate.  In particular, ``m2_totales``
    is a land candidate for houses/parcels/sites, but is only a built-area
    fallback for departments and is never silently copied into built area for a
    house.
    """
    if kind == "built":
        if tipo == "departamento":
            # Department surface priority is explicit built area, then useful
            # area. Do not reinterpret m2_totales or unrelated text as area.
            root_fields = [
                "m2_construidos",
                "superficie_construida_m2",
                "superficie_construida",
                "superficie_util_m2",
                "superficie_util",
            ]
        else:
            root_fields = [
                "m2_construidos",
                "superficie_construida_m2",
                "superficie_util_m2",
                "superficie_util",
                "superficie_construida",
            ]
    else:
        root_fields = ["superficie_terreno_m2", "superficie_terreno", "m2_terreno"]
        if tipo != "departamento":
            root_fields.extend(["m2_totales", "superficie_m2"])

    raw_field_present = False
    for source_name, mapping in _surface_source_maps(doc):
        for field in root_fields if source_name == "root" else ():
            if field not in mapping:
                continue
            raw_value = mapping.get(field)
            if raw_value not in (None, ""):
                raw_field_present = True
            value = valid_surface(raw_value, land=kind == "land")
            if value is not None:
                return {"value": value, "source": f"{source_name}.{field}", "raw_field_present": True}
        if source_name != "root":
            value, present, key = _mapping_surface_candidate(mapping, kind, tipo)
            raw_field_present = raw_field_present or present
            if value is not None:
                return {"value": value, "source": f"{source_name}.{key}", "raw_field_present": True}

    if tipo != "departamento":
        raw_text = " ".join(str(doc.get(k) or "") for k in ("title", "description", "body_text_excerpt"))
        text_value = extract_area_from_text(raw_text, land=kind == "land")
        if text_value is not None:
            return {"value": text_value, "source": "text_pattern", "raw_field_present": raw_field_present}
    return {"value": None, "source": None, "raw_field_present": raw_field_present}


def normalize_market(doc: dict[str, Any]) -> dict[str, Any]:
    operation = normalize_operation(doc.get("operacion"))
    type_name = normalize_type(doc.get("tipo_propiedad"))
    comuna_slug = slugify(doc.get("comuna_slug") or doc.get("comuna"))
    built_audit = extract_market_surface(doc, "built", type_name)
    land_audit = extract_market_surface(doc, "land", type_name)
    built = built_audit["value"]
    useful = None
    if type_name == "departamento":
        source = str(built_audit.get("source") or "")
        useful = built if "util" in source.casefold() else None
        if useful is not None:
            built = None
    land = land_audit["value"]
    if type_name in {"parcela", "sitio"} and land is None:
        fallback = valid_surface(doc.get("m2_construidos"), land=True)
        if fallback is not None:
            land = fallback
            land_audit = {"value": fallback, "source": "root.m2_construidos_as_land_fallback", "raw_field_present": True}
    if type_name == "departamento":
        surface_ref = built or useful
    elif type_name in SIMPLE_SURFACE_TYPES:
        surface_ref = built or land
    else:
        surface_ref = None
    price = normalize_price(doc, operation)
    key = str(doc.get("listing_id") or doc.get("url") or doc.get("_id"))
    built_before = valid_surface(doc.get("m2_construidos"))
    land_before = valid_surface(doc.get("m2_totales"), land=True) if type_name != "departamento" else None
    return {
        "listing_id": key,
        "portal": doc.get("source_portal") or doc.get("source") or "N/A",
        "url": doc.get("canonical_url") or doc.get("url") or "",
        "operation": operation,
        "tipo_normalizado": type_name,
        "comuna_slug": comuna_slug or "N/A",
        "region": canonical_region(comuna_slug, doc.get("region")),
        "price": price,
        "built_m2": built,
        "useful_m2": useful,
        "land_m2": land,
        "built_before_m2": built_before,
        "land_before_m2": land_before,
        "built_source": built_audit["source"],
        "land_source": land_audit["source"],
        "built_raw_field_present": built_audit["raw_field_present"],
        "land_raw_field_present": land_audit["raw_field_present"],
        "bedrooms": valid_count(doc.get("dormitorios")),
        "bathrooms": valid_count(doc.get("banos")),
        "parking": valid_count(doc.get("estacionamientos")),
        "surface_ref_m2": surface_ref,
        "validation_status": doc.get("html_validation_status") or "",
    }


def portfolio_query() -> dict[str, Any]:
    return {
        "disponible_prop360": True,
        "$and": [
            {"$or": [{"estado.estado_prop360": "Activa"}, {"resumen.estado_prop360": "Activa"}]},
            {"$or": [{"estado.oficina": OFFICE}, {"resumen.oficina": OFFICE}, {"oficina_nombre": OFFICE}]},
        ],
    }


def market_projection(*, include_text: bool = False) -> dict[str, int]:
    projection = {
        "listing_id": 1,
        "_id": 1,
        "url": 1,
        "canonical_url": 1,
        "source_portal": 1,
        "source": 1,
        "operacion": 1,
        "tipo_propiedad": 1,
        "comuna": 1,
        "comuna_slug": 1,
        "region": 1,
        "precio_uf": 1,
        "precio_uf_normalizado": 1,
        "precio_clp": 1,
        "precio_clp_normalizado": 1,
        "uf_valor_usado": 1,
        "m2_construidos": 1,
        "m2_totales": 1,
        "dormitorios": 1,
        "banos": 1,
        "estacionamientos": 1,
        "attributes": 1,
        "raw_attributes": 1,
        "html_validation_status": 1,
    }
    if include_text:
        projection.update({"title": 1, "description": 1, "body_text_excerpt": 1})
    return projection


def feature_candidates(tipo: str, market: list[dict[str, Any]], portfolio: dict[str, Any]) -> tuple[list[str], str | None]:
    if tipo == "casa":
        built_cov = sum(m.get("built_m2") is not None for m in market) / max(len(market), 1)
        land_cov = sum(m.get("land_m2") is not None for m in market) / max(len(market), 1)
        if portfolio.get("built_m2") is not None and built_cov >= 0.50:
            features = ["built_m2"]
            if portfolio.get("land_m2") is not None and land_cov >= 0.50:
                features.append("land_m2")
            surface = "built_m2"
        elif land_cov >= 0.50:
            features = ["land_m2"]
            surface = "land_m2"
        elif built_cov >= 0.50:
            features = ["built_m2"]
            surface = "built_m2"
        else:
            return [], None
        for name in ("bedrooms", "bathrooms", "parking"):
            coverage = sum(m.get(name) is not None for m in market) / max(len(market), 1)
            if coverage >= (0.60 if name != "parking" else 0.70):
                features.append(name)
        return features, surface
    if tipo == "departamento":
        valid_areas = [m for m in market if m.get("surface_ref_m2") is not None]
        if not valid_areas:
            return [], None
        features = ["surface_ref_m2"]
        for name in ("bedrooms", "bathrooms", "parking"):
            coverage = sum(m.get(name) is not None for m in market) / max(len(market), 1)
            if coverage >= (0.60 if name != "parking" else 0.70):
                features.append(name)
        return features, "surface_ref_m2"
    if tipo in {"parcela", "sitio"}:
        if sum(m.get("land_m2") is not None for m in market) / max(len(market), 1) < 0.50:
            return [], None
        return ["land_m2"], "land_m2"
    if tipo in SIMPLE_SURFACE_TYPES:
        coverage = sum(m.get("surface_ref_m2") is not None for m in market) / max(len(market), 1)
        if coverage < 0.50:
            return [], None
        return ["surface_ref_m2"], "surface_ref_m2"
    return [], None


def choose_k(features: list[str], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(rows) < 5 or not features:
        return None
    cache_key = (tuple(features), tuple(str(row.get("listing_id")) for row in rows))
    if cache_key in _K_CACHE:
        return _K_CACHE[cache_key]
    matrix = np.array([[float(row[f]) for f in features] for row in rows], dtype=float)
    transformed = matrix.copy()
    for index, feature in enumerate(features):
        if feature.endswith("_m2"):
            transformed[:, index] = np.log1p(np.maximum(transformed[:, index], 0))
    scaled = StandardScaler().fit_transform(transformed)
    candidates: list[dict[str, Any]] = []
    for k in range(2, 6):
        if len(rows) < k * 3:
            continue
        model = KMeans(n_clusters=k, random_state=42, n_init=10)
        labels = model.fit_predict(scaled)
        sizes = Counter(int(x) for x in labels)
        if min(sizes.values()) < 3 or len(sizes) < 2:
            continue
        if len(rows) > SILHOUETTE_SAMPLE_SIZE:
            sample_idx = np.linspace(0, len(rows) - 1, SILHOUETTE_SAMPLE_SIZE, dtype=int)
            score = float(silhouette_score(scaled[sample_idx], labels[sample_idx]))
        else:
            score = float(silhouette_score(scaled, labels))
        candidates.append({"k": k, "model": model, "labels": labels, "sizes": sizes, "silhouette": score, "scaled": scaled})
    if not candidates:
        _K_CACHE[cache_key] = None
        return None
    selected = max(candidates, key=lambda x: (x["silhouette"], -x["k"]))
    _K_CACHE[cache_key] = selected
    return selected


def percentile(value: float | None, values: Iterable[float]) -> float | None:
    if value is None:
        return None
    numbers = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not numbers:
        return None
    less = sum(v < value for v in numbers)
    equal = sum(v == value for v in numbers)
    return round(100.0 * (less + 0.5 * equal) / len(numbers), 2)


def stats(values: list[float], property_value: float | None) -> dict[str, Any]:
    if not values:
        return {"n": 0, "p10": None, "p25": None, "median": None, "p75": None, "p90": None, "property_percentile": None}
    percentiles = np.percentile(np.asarray(values, dtype=float), [10, 25, 50, 75, 90])
    return {
        "n": len(values),
        "p10": round(float(percentiles[0]), 4),
        "p25": round(float(percentiles[1]), 4),
        "median": round(float(percentiles[2]), 4),
        "p75": round(float(percentiles[3]), 4),
        "p90": round(float(percentiles[4]), 4),
        "property_percentile": percentile(property_value, values),
    }


def comparable_payload(row: dict[str, Any], distance: float) -> dict[str, Any]:
    price = row.get("price") or {}
    return {
        "listing_id": row.get("listing_id"),
        "portal": row.get("portal"),
        "url": row.get("url"),
        "precio_uf": price.get("price_uf"),
        "precio_clp": price.get("price_clp"),
        "superficie_construida": row.get("built_m2"),
        "superficie_terreno": row.get("land_m2"),
        "dormitorios": row.get("bedrooms"),
        "banos": row.get("bathrooms"),
        "estacionamientos": row.get("parking"),
        "precio_m2": row.get("price_m2"),
        "precio_m2_construido": row.get("price_m2_built"),
        "precio_m2_terreno": row.get("price_m2_land"),
        "uf_m2_built": row.get("price_m2_built") if price.get("price_uf") is not None else None,
        "uf_m2_land": row.get("price_m2_land") if price.get("price_uf") is not None else None,
        "distance": round(float(distance), 6),
    }


def quality_level(n_clean: int, n_cluster: int, reasons: list[str]) -> str:
    if n_clean >= 20 and n_cluster >= 10:
        return "HIGH"
    if n_clean >= 10 and n_cluster >= 5:
        return "MEDIUM"
    if 5 <= n_clean <= 9:
        return "LOW"
    return "INSUFFICIENT"


def model_group(
    portfolio: dict[str, Any],
    raw_market: list[dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    tipo = portfolio["tipo_normalizado"]
    operation = portfolio["operation"]
    reasons: list[str] = []
    base = {
        "n_raw": len(raw_market),
        "n_clean": 0,
        "n_model": 0,
        "n_cluster": 0,
        "features_used": [],
        "k_tested": [2, 3, 4, 5],
        "k_selected": None,
        "silhouette": None,
        "cluster_sizes": {},
    }
    base["n_with_built_raw_field"] = sum(bool(row.get("built_raw_field_present")) for row in raw_market)
    base["n_with_built_normalized"] = sum(row.get("built_m2") is not None for row in raw_market)
    base["n_with_land_raw_field"] = sum(bool(row.get("land_raw_field_present")) for row in raw_market)
    base["n_with_land_normalized"] = sum(row.get("land_m2") is not None for row in raw_market)
    if operation == "venta_arriendo":
        reasons.append("propiedad_con_dos_operaciones; no se mezclan venta y arriendo en V1")
        return {"status": "INSUFFICIENT_PROPERTY_DATA", "base": base, "reasons": reasons}
    if operation not in {"venta", "arriendo"}:
        reasons.append("operacion_no_normalizada")
        return {"status": "INSUFFICIENT_PROPERTY_DATA", "base": base, "reasons": reasons}
    if tipo not in SUPPORTED_TYPES:
        reasons.append("tipo_no_soportado_por_el_MVP")
        return {"status": "UNSUPPORTED_MVP_TYPE", "base": base, "reasons": reasons}
    if tipo in SIMPLE_SURFACE_TYPES:
        for row in raw_market:
            row["surface_ref_m2"] = row.get("built_m2") or row.get("useful_m2") or row.get("land_m2")

    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for row in raw_market:
        key = (str(row.get("portal") or "N/A"), str(row.get("listing_id")))
        if row.get("validation_status") in {"BLOCKED", "LISTING_REMOVED"}:
            continue
        if not (row.get("price") or {}).get("valid"):
            continue
        if key not in dedup:
            dedup[key] = row
    clean = list(dedup.values())
    base["n_clean"] = len(clean)
    if len(clean) < 5:
        reasons.append("menos_de_5_publicaciones_validas")
        return {"status": "INSUFFICIENT_COMPARABLES", "base": base, "reasons": reasons}

    features, price_surface = feature_candidates(tipo, clean, portfolio)
    if not features or price_surface is None:
        reasons.append("sin_cobertura_suficiente_de_superficie_estructural")
        return {"status": "INSUFFICIENT_COMPARABLES", "base": base, "reasons": reasons}
    if tipo == "casa" and price_surface == "land_m2" and portfolio.get("built_m2") is not None:
        reasons.append("terreno_como_price_surface_por_cobertura_de_construido_insuficiente_despues_de_todas_las_fuentes")
    base["features_used"] = features

    rows = [row for row in clean if all(row.get(feature) is not None for feature in features)]
    base["n_model"] = len(rows)
    if len(rows) < 5:
        reasons.append("menos_de_5_observaciones_completas_para_modelo")
        return {"status": "INSUFFICIENT_COMPARABLES", "base": base, "reasons": reasons}
    if portfolio.get(price_surface) is None:
        reasons.append(f"falta_atributo_clave:{price_surface}")
        return {"status": "INSUFFICIENT_PROPERTY_DATA", "base": base, "reasons": reasons}
    if not (portfolio.get("price") or {}).get("valid"):
        reasons.append("precio_de_cartera_faltante_o_invalido")
        return {"status": "INSUFFICIENT_PROPERTY_DATA", "base": base, "reasons": reasons}
    if any(portfolio.get(feature) is None for feature in features):
        reasons.append("faltan_features_estructurales_de_cartera")
        return {"status": "INSUFFICIENT_PROPERTY_DATA", "base": base, "reasons": reasons}

    selected = choose_k(features, rows)
    if selected is None:
        reasons.append("no_hay_K_con_clusters_minimos_de_3")
        return {"status": "MODEL_UNSTABLE", "base": base, "reasons": reasons}

    base["k_selected"] = selected["k"]
    base["silhouette"] = round(selected["silhouette"], 6)
    base["cluster_sizes"] = {str(k): int(v) for k, v in selected["sizes"].items()}
    matrix = np.array([[float(row[f]) for f in features] for row in rows], dtype=float)
    transformed = matrix.copy()
    for index, feature in enumerate(features):
        if feature.endswith("_m2"):
            transformed[:, index] = np.log1p(np.maximum(transformed[:, index], 0))
    scaler = StandardScaler().fit(transformed)
    portfolio_matrix = np.array([[float(portfolio[f]) for f in features]], dtype=float)
    for index, feature in enumerate(features):
        if feature.endswith("_m2"):
            portfolio_matrix[:, index] = np.log1p(np.maximum(portfolio_matrix[:, index], 0))
    portfolio_scaled = scaler.transform(portfolio_matrix)
    portfolio_cluster = int(selected["model"].predict(portfolio_scaled)[0])
    selected_rows = []
    for row, label, scaled_row in zip(rows, selected["labels"], selected["scaled"]):
        if int(label) == portfolio_cluster:
            comparable = dict(row)
            comparable["distance"] = float(np.linalg.norm(scaled_row - portfolio_scaled[0]))
            selected_rows.append(comparable)
    base["n_cluster"] = len(selected_rows)
    if len(selected_rows) < 3:
        reasons.append("cluster_asignado_con_menos_de_3_observaciones")
        return {"status": "MODEL_UNSTABLE", "base": base, "reasons": reasons}

    price_field = "price_uf" if operation == "venta" else "price_clp"
    for row in selected_rows:
        unit_price = (row.get("price") or {}).get(price_field, 0)
        row["price_m2_built"] = unit_price / row["built_m2"] if row.get("built_m2") else None
        row["price_m2_land"] = unit_price / row["land_m2"] if row.get("land_m2") else None
        row["price_m2"] = unit_price / row[price_surface]
    property_price = (portfolio.get("price") or {}).get(price_field)
    property_price_m2 = property_price / portfolio[price_surface] if property_price else None
    property_price_m2_built = property_price / portfolio["built_m2"] if property_price and portfolio.get("built_m2") else None
    property_price_m2_land = property_price / portfolio["land_m2"] if property_price and portfolio.get("land_m2") else None
    cluster_prices = [(row.get("price") or {}).get(price_field) for row in selected_rows]
    cluster_prices = [value for value in cluster_prices if value is not None]
    cluster_price_m2 = [row.get("price_m2") for row in selected_rows if row.get("price_m2") is not None]
    cluster_price_m2_built = [row.get("price_m2_built") for row in selected_rows if row.get("price_m2_built") is not None]
    cluster_price_m2_land = [row.get("price_m2_land") for row in selected_rows if row.get("price_m2_land") is not None]
    nearest = sorted(selected_rows, key=lambda row: row["distance"])[:5]
    quality_reasons = list(reasons)
    if price_surface == "land_m2" and tipo == "casa" and portfolio.get("built_m2") is not None:
        quality_reasons.append("precio_m2_principal_usa_terreno_por_cobertura_de_construido_insuficiente")
    level = quality_level(base["n_clean"], base["n_cluster"], quality_reasons)
    return {
        "status": "CLUSTERED",
        "base": base,
        "reasons": quality_reasons,
        "cluster_id": portfolio_cluster,
        "price_surface": price_surface,
        "price_stats": stats(cluster_prices, property_price),
        "price_m2_stats": stats(cluster_price_m2, property_price_m2),
        "price_m2_built_stats": stats(cluster_price_m2_built, property_price_m2_built),
        "price_m2_land_stats": stats(cluster_price_m2_land, property_price_m2_land),
        "comparables": [comparable_payload(row, row["distance"]) for row in nearest],
        "quality_level": level,
    }


def build_result(portfolio: dict[str, Any], raw_market: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    modeled = model_group(portfolio, raw_market, generated_at)
    base = modeled["base"]
    status = modeled["status"]
    quality = modeled.get("quality_level", "INSUFFICIENT")
    quality_reasons = list(modeled.get("reasons", []))
    if status != "CLUSTERED":
        quality_reasons.append(f"status:{status}")
    price = portfolio.get("price") or {}
    property_unit_price = price.get("price_uf") if portfolio.get("operation") == "venta" else price.get("price_clp")
    property_m2_built = property_unit_price / portfolio["built_m2"] if property_unit_price and portfolio.get("built_m2") else None
    property_m2_land = property_unit_price / portfolio["land_m2"] if property_unit_price and portfolio.get("land_m2") else None
    property_payload = {
        "codigo": portfolio.get("codigo"),
        "operacion": portfolio.get("operation"),
        "tipo_original": portfolio.get("tipo_original"),
        "precio_uf": price.get("price_uf") if price.get("valid") else None,
        "precio_clp": price.get("price_clp") if price.get("valid") else None,
        "superficie_construida_m2": portfolio.get("built_m2"),
        "superficie_util_m2": portfolio.get("useful_m2"),
        "superficie_terreno_m2": portfolio.get("land_m2"),
        "dormitorios": portfolio.get("bedrooms"),
        "banos": portfolio.get("bathrooms"),
        "estacionamientos": portfolio.get("parking"),
        "precio_m2_construido": property_m2_built,
        "precio_m2_terreno": property_m2_land,
    }
    result = {
        "codigo": portfolio.get("codigo"),
        "model_version": MODEL_VERSION,
        "generated_at": generated_at,
        "oficina": OFFICE,
        "status": status,
        "segmento": {
            "operacion": portfolio.get("operation"),
            "tipo_normalizado": portfolio.get("tipo_normalizado"),
            "comuna_slug": portfolio.get("comuna_slug"),
            "region": portfolio.get("region"),
        },
        "propiedad": property_payload,
        "cohort": {
            "key": f"{portfolio.get('operation')}|{portfolio.get('tipo_normalizado')}|{portfolio.get('comuna_slug')}",
            "operation": portfolio.get("operation"),
            "tipo_normalizado": portfolio.get("tipo_normalizado"),
            "comuna_slug": portfolio.get("comuna_slug"),
        },
        "cluster": {
            "id": modeled.get("cluster_id"),
            "k": base.get("k_selected"),
            "silhouette": base.get("silhouette"),
            "features_used": base.get("features_used", []),
            "size": base.get("n_cluster", 0),
            "sizes": base.get("cluster_sizes", {}),
        },
        "mercado_cluster": {
            "n_raw": base.get("n_raw", 0),
            "n_clean": base.get("n_clean", 0),
            "n_model": base.get("n_model", 0),
            "n_cluster": base.get("n_cluster", 0),
            "n_with_built_raw_field": base.get("n_with_built_raw_field", 0),
            "n_with_built_normalized": base.get("n_with_built_normalized", 0),
            "n_with_land_raw_field": base.get("n_with_land_raw_field", 0),
            "n_with_land_normalized": base.get("n_with_land_normalized", 0),
            "price_surface": modeled.get("price_surface"),
            "price": modeled.get("price_stats"),
            "price_m2": modeled.get("price_m2_stats"),
            "price_m2_built": modeled.get("price_m2_built_stats"),
            "price_m2_land": modeled.get("price_m2_land_stats"),
            "price_unit": "UF" if portfolio.get("operation") == "venta" else "CLP",
            "publication_disclaimer": "Publicaciones comparables observadas en el mercado; no son precios de cierre ni transacciones.",
        },
        "comparables": modeled.get("comparables", []),
        "data_quality": {"level": quality, "reasons": quality_reasons},
        "audit": {
            "source_collections": [PORTFOLIO_COLLECTION, MARKET_COLLECTION],
            "uf_m2_cache_used": False,
            "source_documents_modified": False,
            "generated_by": "analytics/prop360_comparables_cluster_mvp.py",
        },
    }
    return result


def audit_sources(db: Any) -> dict[str, Any]:
    portfolio = db[PORTFOLIO_COLLECTION]
    market = db[MARKET_COLLECTION]
    exact = list(portfolio.find(portfolio_query(), {"_id": 0, "codigo": 1, "tipo_operacion": 1, "metadata": 1, "ubicacion": 1, "caracteristicas": 1, "estado": 1, "resumen": 1}).limit(5000))
    op_counts = Counter()
    type_counts = Counter()
    for doc in exact:
        normalized = normalize_portfolio(doc)
        op = normalized["operation"]
        op_counts["Venta" if op == "venta" else "Arriendo" if op == "arriendo" else "Venta, Arriendo" if op == "venta_arriendo" else "Otros"] += 1
        type_counts[normalized["tipo_normalizado"]] += 1
    return {
        "portfolio_count": len(exact),
        "market_count": market.count_documents({}),
        "operation_distribution": dict(op_counts),
        "type_distribution": dict(type_counts),
        "expected_portfolio_count": 444,
        "discrepancies": {
            "count": len(exact) != 444,
            "operation_distribution_approximate": dict(op_counts) != {"Venta": 382, "Arriendo": 60, "Venta, Arriendo": 2},
            "type_distribution_approximate": {"oficina": type_counts.get("oficina", 0), "sitio": type_counts.get("sitio", 0)},
        },
    }


def update_portal_coverage(coverage: dict[str, dict[str, Counter]], row: dict[str, Any]) -> None:
    portal = str(row.get("portal") or "N/A").casefold()
    tipo = str(row.get("tipo_normalizado") or "N/A")
    counter = coverage.setdefault(portal, {}).setdefault(tipo, Counter())
    counter["n_total"] += 1
    counter["n_superficie_construida_before"] += row.get("built_before_m2") is not None
    counter["n_superficie_construida_raw_field"] += bool(row.get("built_raw_field_present"))
    counter["n_superficie_construida_after"] += row.get("built_m2") is not None
    counter["n_superficie_terreno_before"] += row.get("land_before_m2") is not None
    counter["n_superficie_terreno_raw_field"] += bool(row.get("land_raw_field_present"))
    counter["n_superficie_terreno_after"] += row.get("land_m2") is not None
    counter["n_dormitorios"] += row.get("bedrooms") is not None
    counter["n_banos"] += row.get("bathrooms") is not None
    counter["n_precio_valido"] += bool((row.get("price") or {}).get("valid"))


def serialize_coverage(coverage: dict[str, dict[str, Counter]]) -> dict[str, dict[str, dict[str, int]]]:
    return {
        portal: {tipo: {key: int(value) for key, value in counter.items()} for tipo, counter in by_type.items()}
        for portal, by_type in coverage.items()
    }


def load_data(
    db: Any, *, pilot_only: bool = False
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    audit = audit_sources(db)
    portfolio_filter = {"codigo": "7390"} if pilot_only else portfolio_query()
    portfolio_docs = list(db[PORTFOLIO_COLLECTION].find(portfolio_filter, {"_id": 0, "codigo": 1, "tipo_operacion": 1, "metadata": 1, "ubicacion": 1, "caracteristicas": 1, "estado": 1, "resumen": 1}).sort("codigo", 1))
    normalized_portfolio = [normalize_portfolio(doc) for doc in portfolio_docs]
    market_filter = {"comuna_slug": "chillan"} if pilot_only else {}
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    coverage: dict[str, dict[str, Counter]] = {}
    # Pilot and batch must use exactly the same field-based extractor.  Text
    # remains an optional helper in the function, but is not used for the
    # production cohort audit because it is not one of the auditable fields.
    market_cursor = db[MARKET_COLLECTION].find(market_filter, market_projection(include_text=False)).batch_size(1000)
    for raw in market_cursor:
        row = normalize_market(raw)
        update_portal_coverage(coverage, row)
        if row["operation"] in {"venta", "arriendo"} and row["tipo_normalizado"] != "N/A" and row["comuna_slug"] != "N/A":
            key = f"{row['operation']}|{row['tipo_normalizado']}|{row['comuna_slug']}"
            by_cohort[key].append(row)
    audit["market_coverage_by_portal_type"] = serialize_coverage(coverage)
    return normalized_portfolio, by_cohort, audit


def print_pilot(results: list[dict[str, Any]], audit: dict[str, Any]) -> None:
    pilot = next((result for result in results if result.get("codigo") == "7390"), None)
    print("SOURCE_AUDIT=" + repr(audit))
    if not pilot:
        print("PILOT_7390_STATUS=NOT_FOUND")
        return
    market = pilot.get("mercado_cluster") or {}
    price = market.get("price") or {}
    price_m2_built = market.get("price_m2_built") or {}
    price_m2_land = market.get("price_m2_land") or {}
    property_data = pilot.get("propiedad") or {}
    print("7390_STATUS=" + str(pilot.get("status")))
    print("7390_N_RAW=" + str(market.get("n_raw", 0)))
    print("7390_N_WITH_BUILT_RAW_FIELD=" + str(market.get("n_with_built_raw_field", 0)))
    print("7390_N_WITH_BUILT_NORMALIZED=" + str(market.get("n_with_built_normalized", 0)))
    print("7390_FEATURES_USED=" + repr((pilot.get("cluster") or {}).get("features_used", [])))
    print("7390_K=" + str((pilot.get("cluster") or {}).get("k")))
    print("7390_CLUSTER_N=" + str(market.get("n_cluster", 0)))
    print("7390_PRICE_SURFACE=" + str(market.get("price_surface")))
    print("7390_PRICE_PERCENTILE=" + str(price.get("property_percentile")))
    print("7390_UF_M2_BUILT=" + str(property_data.get("precio_m2_construido")))
    print("7390_UF_M2_BUILT_PERCENTILE=" + str(price_m2_built.get("property_percentile")))
    print("7390_UF_M2_LAND=" + str(property_data.get("precio_m2_terreno")))
    print("7390_UF_M2_LAND_PERCENTILE=" + str(price_m2_land.get("property_percentile")))
    cluster = pilot.get("cluster") or {}
    print("7390_SILHOUETTE=" + str(cluster.get("silhouette")))
    print("7390_CLUSTER_SIZES=" + repr(cluster.get("sizes", {})))
    print("7390_QUALITY=" + str((pilot.get("data_quality") or {}).get("level")))
    print("7390_REASONS=" + repr((pilot.get("data_quality") or {}).get("reasons")))
    comparable_view = [
        {
            "listing_id": row.get("listing_id"),
            "portal": row.get("portal"),
            "precio_uf": row.get("precio_uf"),
            "m2_construido": row.get("superficie_construida"),
            "m2_terreno": row.get("superficie_terreno"),
            "dormitorios": row.get("dormitorios"),
            "banos": row.get("banos"),
            "uf_m2_construido": row.get("uf_m2_built"),
            "uf_m2_terreno": row.get("uf_m2_land"),
            "distance": row.get("distance"),
        }
        for row in (pilot.get("comparables") or [])[:5]
    ]
    print("7390_COMPARABLES=" + json.dumps(comparable_view, ensure_ascii=False, default=str))
    coverage = audit.get("market_coverage_by_portal_type") or {}
    for portal in ("yapo", "chilepropiedades", "toctoc"):
        for tipo, values in sorted((coverage.get(portal) or {}).items()):
            print(f"COVERAGE portal={portal} tipo={tipo} values=" + json.dumps(values, ensure_ascii=False, sort_keys=True))


def write_results(db: Any, results: list[dict[str, Any]]) -> None:
    collection = db[OUTPUT_COLLECTION]
    operations = [
        UpdateOne(
            {"codigo": result["codigo"], "model_version": MODEL_VERSION},
            {"$set": result},
            upsert=True,
        )
        for result in results
    ]
    if operations:
        for start in range(0, len(operations), 250):
            collection.bulk_write(operations[start : start + 250], ordered=False)
    collection.create_index([("codigo", 1), ("model_version", 1)], unique=True, name="codigo_model_version_unique")
    collection.create_index([("status", 1)], name="status_idx")
    collection.create_index([("segmento.tipo_normalizado", 1)], name="segmento_tipo_idx")
    collection.create_index([("segmento.comuna_slug", 1)], name="segmento_comuna_idx")


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(result.get("status") for result in results)
    quality = Counter((result.get("data_quality") or {}).get("level") for result in results)
    return {
        "output_count": len(results),
        "statuses": dict(statuses),
        "quality": dict(quality),
        "model_version": MODEL_VERSION,
        "collection_created": OUTPUT_COLLECTION,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MVP Prop360 clustering/comparables")
    parser.add_argument("--pilot-only", action="store_true", help="audita y calcula 7390 sin escribir Mongo")
    parser.add_argument("--dry-run", action="store_true", help="calcula toda la cartera sin escribir Mongo")
    args = parser.parse_args()
    if not Config.MONGO_URI:
        raise RuntimeError("MONGO_URI no está configurado")
    client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=20_000, connectTimeoutMS=20_000)
    client.admin.command("ping")
    db = client[Config.DB_NAME]
    normalized_portfolio, by_cohort, audit = load_data(db, pilot_only=args.pilot_only)
    generated_at = datetime.now(timezone.utc).isoformat()
    results = []
    for portfolio in normalized_portfolio:
        key = f"{portfolio['operation']}|{portfolio['tipo_normalizado']}|{portfolio['comuna_slug']}"
        results.append(build_result(portfolio, by_cohort.get(key, []), generated_at))
    print_pilot(results, audit)
    if args.pilot_only:
        print("WRITE_PERFORMED=NO")
        return 0
    if not args.dry_run:
        write_results(db, results)
        print("WRITE_PERFORMED=YES")
    else:
        print("WRITE_PERFORMED=NO")
    print("SUMMARY=" + repr(summarize(results)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
