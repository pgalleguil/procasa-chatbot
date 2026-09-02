"""Campos derivados y estables para acelerar el listado de Captaciones.

Las funciones de este módulo reproducen las expresiones que históricamente
se calculaban dentro de ``get_captacion_list``. No definen nuevas métricas de
negocio: solo dejan persistido el mismo valor que se usaba para ordenar.
"""

from datetime import datetime, timezone
import re

from captacion_kpis import AVAILABLE_STATES


CAPTACION_SORT_DATE_FIELDS = (
    "first_seen", "first_seen_at", "created_at", "fecha_captura",
    "processed_at", "scraped_at", "updated_at",
)
CAPTACION_PRICE_UF_FIELDS = (
    "precio_uf", "price_uf", "details.precio_uf", "details.price_uf",
    "precio_raw", "price", "precio", "details.precio",
)
CAPTACION_PRICE_CLP_FIELDS = (
    "precio_clp", "price_clp", "details.precio_clp", "details.price_clp",
    "precio_raw", "price", "precio", "details.precio",
)


def _get_path(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _first_value(document, fields):
    for field in fields:
        value = _get_path(document, field)
        if value is not None and value != "":
            return value
    return None


def normalize_sort_number(value, positive=False):
    """Replica el parser Mongo usado por el listado actual.

    En particular, si el texto contiene coma se interpreta como formato
    chileno (puntos de miles y coma decimal); si no, se conserva el punto.
    """
    if value is None:
        return None
    cleaned = str(value)
    for token in ("UF", "$", " "):
        cleaned = cleaned.replace(token, "")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        parsed = float(cleaned)
    except (TypeError, ValueError):
        return None
    if positive and parsed <= 0:
        return None
    return parsed


def normalize_sort_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_phone(value):
    if value is None:
        return ""
    return "".join(char for char in str(value) if char.isdigit())


def build_captacion_materialized_fields(document):
    """Return only derived fields used by indexed list filters/sorts."""
    document = document or {}
    gestion = document.get("gestion") or {}
    estado = gestion.get("estado") or "NUEVO"

    precio_uf = None
    for field in CAPTACION_PRICE_UF_FIELDS:
        precio_uf = normalize_sort_number(_get_path(document, field), positive=True)
        if precio_uf is not None:
            break

    precio_clp = None
    for field in CAPTACION_PRICE_CLP_FIELDS:
        precio_clp = normalize_sort_number(_get_path(document, field), positive=True)
        if precio_clp is not None:
            break

    sort_date = None
    for field in CAPTACION_SORT_DATE_FIELDS:
        sort_date = normalize_sort_date(_get_path(document, field))
        if sort_date is not None:
            break
    management_date = normalize_sort_date(gestion.get("fecha_ultima_gestion"))

    comuna = _first_value(document, ("comuna_slug", "details.comuna_norm", "comuna"))
    probability = normalize_sort_number(
        _get_path(document, "classification.owner_probability")
    )
    phone = _first_value(document, (
        "contact_phone", "whatsapp_phone", "telefono",
        "details.whatsapp_phone", "details.contact_phone", "details.telefono",
        "details.phone",
    ))

    return {
        "captacion_sort_date": sort_date,
        "captacion_management_date": management_date,
        "captacion_priority": 0 if estado in AVAILABLE_STATES else 1,
        "precio_uf_normalizado": precio_uf,
        "precio_clp_normalizado": precio_clp,
        "captacion_price_sort": precio_uf if precio_uf is not None else precio_clp if precio_clp is not None else -1,
        "captacion_probability_sort": probability if probability is not None else -1,
        "captacion_comuna_sort": str(comuna or "").lower(),
        "telefono_normalizado": normalize_phone(phone),
    }


def materialized_field_names():
    return tuple(build_captacion_materialized_fields({}).keys())
