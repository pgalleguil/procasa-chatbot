import re
from typing import Any, Dict, Iterable, Optional

from config import Config
from .utils import safe_int_conversion

PROPERTY_COLLECTION_NAME = "universo_cartera_pro360"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _regex(value: str) -> Dict[str, Any]:
    return {"$regex": re.escape(value), "$options": "i"}


def _non_empty(values: Iterable[Any]) -> list[Any]:
    return [v for v in values if v not in (None, "", [])]


def build_property_lookup_queries(raw_value: Any) -> list[Dict[str, Any]]:
    """
    Construye consultas robustas para ubicar una propiedad en la colección
    Prop360 nueva, manteniendo compatibilidad parcial con el esquema antiguo.
    """
    value = _clean_text(raw_value)
    if not value:
        return []

    value_int = safe_int_conversion(value)
    value_lower = value.lower()

    queries: list[Dict[str, Any]] = [
        {"codigo": value},
        {"codigo": value_int},
        {"codigo": {"$in": [value, value_int]}},
        {"publicaciones.yapo.url_yapo": value},
        {"yapo.url_yapo": value},
        {"url_yapo": value},
        {"publicaciones.portal_inmobiliario.url_pi": value},
        {"publicaciones.portal_inmobiliario.url_mercado_libre": value},
        {"publicaciones.procasa.url_procasa": value},
        {"publicaciones.toctoc.url_toctoc": value},
        {"ubicacion.comuna": value},
        {"ubicacion.comuna": _regex(value)},
        {"ubicacion.region": value},
        {"ubicacion.region": _regex(value)},
        {"estado.ejecutivo": value},
        {"estado.ejecutivo": _regex(value)},
        {"publicaciones.procasa.url_procasa": _regex(value)},
        {"metadata.source_url": value},
        {"metadata.source_url": _regex(value)},
        {"source_url": value},
        {"source_url": _regex(value)},
        {"publicaciones.portal_inmobiliario.url_mercado_libre": _regex(value)},
        {"publicaciones.portal_inmobiliario.url_pi": _regex(value)},
        {"publicaciones.toctoc.url_toctoc": _regex(value)},
        {"yapo.url_yapo": _regex(value)},
        {"yapo.codigo_yapo": value},
        {"yapo.codigo_yapo": value_int},
        {"publicaciones.yapo.url_yapo": _regex(value)},
        {"publicaciones.codigo_internacional": value},
        {"publicaciones.codigo_internacional": value_int},
        {"publicaciones.portal_inmobiliario.codigo_pi": value},
        {"publicaciones.portal_inmobiliario.codigo_pi": value_int},
        {"publicaciones.yapo.codigo_yapo": value},
        {"publicaciones.yapo.codigo_yapo": value_int},
        {"codigo_pi": value},
        {"codigo_pi": value_int},
        {"codigo_mercadolibre": value},
        {"codigo_mercadolibre": value_int},
        {"codigo_yapo": value},
        {"codigo_yapo": value_int},
        {"codigo_internacional": value},
        {"codigo_internacional": value_int},
        {"toctoc.enlace": value},
        {"toctoc.enlace": _regex(value)},
    ]

    if "http" in value_lower or ".cl" in value_lower or "/" in value:
        queries.extend([
            {"publicaciones.yapo.url_yapo": value},
            {"yapo.url_yapo": value},
            {"publicaciones.procasa.url_procasa": _regex(value)},
            {"metadata.source_url": _regex(value)},
            {"source_url": _regex(value)},
            {"publicaciones.portal_inmobiliario.url_mercado_libre": _regex(value)},
            {"publicaciones.portal_inmobiliario.url_pi": _regex(value)},
            {"publicaciones.toctoc.url_toctoc": _regex(value)},
            {"yapo.url_yapo": _regex(value)},
            {"publicaciones.yapo.url_yapo": _regex(value)},
        ])

    # Deduplicar por representación para evitar consultas redundantes.
    seen = set()
    unique_queries = []
    for query in queries:
        key = repr(query)
        if key in seen:
            continue
        seen.add(key)
        unique_queries.append(query)
    return unique_queries


def find_property_by_any_identifier(db, raw_value: Any, collection_name: str = PROPERTY_COLLECTION_NAME):
    collection = db[collection_name]
    for query in build_property_lookup_queries(raw_value):
        prop = collection.find_one(query)
        if prop:
            return prop
    return None


def get_prop_location(prop: Dict[str, Any]) -> Dict[str, Any]:
    ubicacion = prop.get("ubicacion", {}) or {}
    return {
        "region": _clean_text(ubicacion.get("region") or prop.get("region")),
        "comuna": _clean_text(ubicacion.get("comuna") or prop.get("comuna")),
        "sector": _clean_text(ubicacion.get("sector") or prop.get("sector")),
        "direccion": _clean_text(
            ubicacion.get("calle")
            or ubicacion.get("direccion_referencial")
            or prop.get("direccion")
            or prop.get("nombre_calle")
        ),
    }


def get_prop_operation(prop: Dict[str, Any]) -> Dict[str, Any]:
    tipo_operacion = prop.get("tipo_operacion", {}) or {}
    return {
        "tipo": _clean_text(tipo_operacion.get("tipo") or prop.get("tipo")),
        "operacion": _clean_text(
            "Venta" if tipo_operacion.get("venta") else "Arriendo" if tipo_operacion.get("arriendo") else prop.get("operacion")
        ),
        "precio_uf": (
            tipo_operacion.get("precio_venta", {}) or {}
        ).get("precio_uf")
        or (tipo_operacion.get("precio_arriendo", {}) or {}).get("precio_uf")
        or prop.get("precio_uf"),
        "precio_clp": (
            tipo_operacion.get("precio_venta", {}) or {}
        ).get("precio_clp")
        or (tipo_operacion.get("precio_arriendo", {}) or {}).get("precio_clp")
        or prop.get("precio_clp"),
    }


def get_prop_executive(prop: Dict[str, Any]) -> str:
    estado = prop.get("estado", {}) or {}
    for key in ("ejecutivo", "captador", "responsable"):
        value = _clean_text(estado.get(key) or prop.get(key))
        if value:
            return value
    return ""
