"""Shared commercial-visibility rules for the two new Yapo Maipú batches.

The rule is intentionally cohort-scoped. Historical captaciones and records
from other portals continue to use their existing visibility criteria.
"""

from __future__ import annotations

from copy import deepcopy
import re
from urllib.parse import urlparse


YAPO_NEW_BATCHES = {
    "maipu_houses_3000_6000_20261103": "yapo_maipu_482_20261103_v3",
    "issue122_maipu_20261104_141023_35cf": "issue122_maipu_20261104_141023_35cf",
}

_CANONICAL_URL_RE = re.compile(
    r"^https?://(?:www\.)?yapo\.cl/.+/[0-9]{6,}/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_PRICE_TEXT_RE = re.compile(
    r"^\s*(?:(?:UF|CLF|CLP)\s*[0-9][0-9., ]*|"
    r"\$\s*[0-9][0-9., ]*(?:\s*CLP)?|"
    r"[0-9][0-9., ]*\s*(?:UF|CLF|CLP))"
    r"(?:\s*-\s*[0-9]+\s*%)?\s*$",
    re.IGNORECASE,
)
_VALID_OPERATION_RE = re.compile(r"^(?:venta|arriendo|alquiler)$", re.IGNORECASE)
_UNKNOWN_TEXT_RE = re.compile(r"^(?:n/?a|n/?d|s/?i|sin informaci[oó]n|sin dato)$", re.IGNORECASE)

_TITLE_PATHS = ("title", "titulo", "details.title", "details.titulo")
_DESCRIPTION_PATHS = ("description", "descripcion", "details.description", "details.descripcion")
_COMMUNE_PATHS = ("comuna", "details.comuna")
_OPERATION_PATHS = ("operacion", "operation", "tipo_operacion", "details.operacion", "details.tipo_operacion")
_PROPERTY_TYPE_PATHS = ("tipo_propiedad", "property_type", "details.tipo_propiedad", "details.tipo")
_PRICE_NUMBER_PATHS = (
    "precio_uf", "price_uf", "details.precio_uf", "details.price_uf",
    "precio_clp", "price_clp", "details.precio_clp", "details.price_clp",
)
_PRICE_TEXT_PATHS = (
    "price", "precio_raw", "price_raw", "precio", "details.price",
    "details.precio_raw", "details.price_raw", "details.precio",
)
_ERROR_PATHS = ("detail_error", "scrape_error", "extraction_error")
_TRUNCATION_PATHS = (
    "descripcion_is_truncated", "description_is_truncated",
    "descripcion_truncated", "description_truncated",
)


def _path_value(document: dict, path: str):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _nonempty(value) -> bool:
    if value is None or isinstance(value, bool):
        return False
    text = str(value).strip()
    return bool(text) and not _UNKNOWN_TEXT_RE.fullmatch(text)


def _has_any_text(document: dict, paths: tuple[str, ...]) -> bool:
    return any(_nonempty(_path_value(document, path)) for path in paths)


def is_new_yapo_cohort(document: dict | None) -> bool:
    """Match only records carrying both exact batch provenance markers."""
    document = document or {}
    if str(document.get("origen") or "").strip() != "yapo":
        return False
    ingestion = document.get("yapo_ingestion")
    if not isinstance(ingestion, dict):
        return False
    source_batch = str(ingestion.get("source_batch_id") or "").strip()
    return YAPO_NEW_BATCHES.get(str(document.get("batch_id") or "").strip()) == source_batch


def _cohort_selector() -> dict:
    return {
        "origen": "yapo",
        "$or": [
            {"batch_id": batch, "yapo_ingestion.source_batch_id": source}
            for batch, source in YAPO_NEW_BATCHES.items()
        ],
    }


def yapo_new_cohort_query() -> dict:
    """Return the exact Mongo selector for the two authorized Yapo batches."""
    return deepcopy(_cohort_selector())


def _nonempty_string_clause(paths: tuple[str, ...]) -> dict:
    return {
        "$or": [
            {
                path: {
                    "$type": "string",
                    "$regex": r"\S",
                    "$not": _UNKNOWN_TEXT_RE,
                }
            }
            for path in paths
        ]
    }


def _valid_price_clause() -> dict:
    return {
        "$or": [
            {path: {"$type": "number", "$gt": 0}}
            for path in _PRICE_NUMBER_PATHS
        ] + [
            {path: {"$type": "string", "$regex": _PRICE_TEXT_RE}}
            for path in _PRICE_TEXT_PATHS
        ]
    }


def _valid_canonical_identity_clause() -> dict:
    # `canonical_url` is extracted from the saved detail HTML. The expression
    # checks that its final listing identifier equals the stored listing_id.
    return {
        "$and": [
            {"url": {"$type": "string", "$regex": _CANONICAL_URL_RE}},
            {"canonical_url": {"$type": "string", "$regex": _CANONICAL_URL_RE}},
            {"listing_id": {"$type": "string", "$regex": r"^[0-9]{6,}$"}},
            {
                "$expr": {
                    "$and": [
                        {
                            "$regexMatch": {
                                "input": {"$ifNull": [path, ""]},
                                "regex": {
                                    "$concat": [
                                        r"/",
                                        {"$toString": "$listing_id"},
                                        r"/?(?:[?#].*)?$",
                                    ]
                                },
                                "options": "i",
                            }
                        }
                        for path in ("$url", "$canonical_url")
                    ]
                }
            },
        ]
    }


def yapo_commercial_quality_query() -> dict:
    """Mongo-compatible evidence required before a new Yapo row is visible."""
    clauses = [
        {"html_validation_status": "OK"},
        {"availability_status": {"$in": ["ACTIVO", "NO_VERIFICADO"]}},
        {"scrape_stage": {"$nin": ["needs_rescrape", "ad_removed"]}},
        {
            "$and": [
                {"$or": [{path: {"$exists": False}}, {path: None}, {path: ""}]}
                for path in _ERROR_PATHS
            ]
        },
        {"$and": [{path: {"$ne": True}} for path in _TRUNCATION_PATHS]},
        _nonempty_string_clause(_TITLE_PATHS),
        _nonempty_string_clause(_DESCRIPTION_PATHS),
        _nonempty_string_clause(_COMMUNE_PATHS),
        {
            "$or": [
                {path: {"$type": "string", "$regex": _VALID_OPERATION_RE}}
                for path in _OPERATION_PATHS
            ]
        },
        _nonempty_string_clause(_PROPERTY_TYPE_PATHS),
        _valid_price_clause(),
        _valid_canonical_identity_clause(),
        {"availability_status": {"$ne": "DESACTIVADO"}},
        {"html_validation_status": {"$ne": "LISTING_REMOVED"}},
    ]
    return {"$and": clauses}


def apply_captacion_visibility(base_query: dict | None = None) -> dict:
    """Add the cohort-only eligibility predicate without mutating caller input.

    Non-cohort records pass through unchanged. New Yapo records must also have
    a validated, correctly identified detail HTML and complete commercial
    fields. The predicate is applied server-side before count/sort/pagination.
    """
    base = deepcopy(base_query or {})
    cohort = _cohort_selector()
    quality = yapo_commercial_quality_query()
    return {
        "$and": [
            base,
            {
                "$or": [
                    {"$nor": [cohort]},
                    {"$and": [cohort, quality]},
                ]
            },
        ]
    }


def build_yapo_visibility_diagnostics_query(classification_states) -> dict:
    """Select only classified-visible cohort rows blocked by extraction quality."""
    return {
        "$and": [
            _cohort_selector(),
            {"classification.state": {"$in": list(classification_states)}},
            {"$nor": [yapo_commercial_quality_query()]},
        ]
    }


def commercial_visibility_exclusion_reasons(document: dict | None) -> list[str]:
    """Explain why a document in the new Yapo cohorts cannot enter the tray."""
    document = document or {}
    if not is_new_yapo_cohort(document):
        return []

    reasons: list[str] = []
    availability = str(document.get("availability_status") or "").strip().upper()
    validation = str(document.get("html_validation_status") or "").strip().upper()
    stage = str(document.get("scrape_stage") or "").strip().casefold()
    if availability == "DESACTIVADO" or validation == "LISTING_REMOVED" or stage == "ad_removed":
        reasons.append("anuncio_retirado_confirmado")
    if availability not in {"ACTIVO", "NO_VERIFICADO", "DESACTIVADO"}:
        reasons.append("disponibilidad_no_interpretable")
    if validation not in {"OK", "LISTING_REMOVED"}:
        reasons.append("html_no_validado")
    if stage == "needs_rescrape":
        reasons.append("extraccion_tecnica_pendiente")
    if any(_nonempty(_path_value(document, path)) for path in _ERROR_PATHS):
        reasons.append("error_tecnico_de_extraccion")
    if any(_path_value(document, path) is True for path in _TRUNCATION_PATHS):
        reasons.append("descripcion_truncada")

    for code, paths in (
        ("titulo_faltante", _TITLE_PATHS),
        ("descripcion_faltante", _DESCRIPTION_PATHS),
        ("comuna_faltante", _COMMUNE_PATHS),
        ("operacion_faltante", _OPERATION_PATHS),
        ("tipo_propiedad_faltante", _PROPERTY_TYPE_PATHS),
    ):
        if not _has_any_text(document, paths):
            reasons.append(code)

    operation_values = [_path_value(document, path) for path in _OPERATION_PATHS]
    if not any(isinstance(value, str) and _VALID_OPERATION_RE.fullmatch(value.strip()) for value in operation_values):
        if "operacion_faltante" not in reasons:
            reasons.append("operacion_no_interpretable")

    if not _has_interpretable_price(document):
        reasons.append("precio_o_moneda_no_interpretable")
    if not _has_matching_canonical_identity(document):
        reasons.append("identidad_html_no_coincide_con_anuncio")
    return list(dict.fromkeys(reasons))


def _has_interpretable_price(document: dict) -> bool:
    for path in _PRICE_NUMBER_PATHS:
        value = _path_value(document, path)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    for path in _PRICE_TEXT_PATHS:
        value = _path_value(document, path)
        if isinstance(value, str) and _PRICE_TEXT_RE.fullmatch(value.strip()):
            return bool(re.search(r"\d", value))
    return False


def _has_matching_canonical_identity(document: dict) -> bool:
    listing_id = str(document.get("listing_id") or "").strip()
    canonical_url = str(document.get("canonical_url") or "").strip()
    url = str(document.get("url") or "").strip()
    if not listing_id.isdigit() or len(listing_id) < 6:
        return False
    if not _CANONICAL_URL_RE.fullmatch(url) or not _CANONICAL_URL_RE.fullmatch(canonical_url):
        return False
    canonical = urlparse(canonical_url)
    source = urlparse(url)
    return all(
        parsed.hostname in {"yapo.cl", "www.yapo.cl"}
        and parsed.path.rstrip("/").split("/")[-1] == listing_id
        for parsed in (canonical, source)
    )


def is_commercially_usable_yapo_document(document: dict | None) -> bool:
    """Python-side counterpart for tests and admin diagnostics."""
    return not commercial_visibility_exclusion_reasons(document)
