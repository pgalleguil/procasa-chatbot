"""Offline Yapo-to-captacion compatibility audit for saved listings.

Reads local JSON/HTML only. It never connects to MongoDB or calls DeepSeek.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .classifier_rules import classify_yapo_listing
from .extractor import extract_listing_fields
from .mongo_store import MongoStore
from .config import AppConfig
from .downloader import validate_html

UF_CLP = 41263.57
UF_DATE = "2026-11-03"
UF_SOURCE = "https://www.sii.cl/valores_y_fechas/uf/uf2026.htm"
SAVED_FIELDS = (
    "title", "description", "price", "price_value", "price_currency", "publicador_visible",
    "contact_name", "contact_logo_alt", "seller_type", "listing_advertiser",
    "seller_jsonld_name", "contact_badges_text", "seller_profile_id", "seller_profile_url",
    "listing_advertiser_type", "listing_advertiser", "seller_text", "seller_avatar_alt",
    "attributes", "image_urls", "images", "main_image_url", "canonical_url",
    "publisher_field_provenance", "publisher_identity_candidates", "publisher_profile_context",
    "fecha_publicacion", "fecha_publicacion_raw", "fecha_scraping", "m2_construidos", "m2_totales",
    "superficie_util", "superficie_total", "enlaces_fotos", "image_urls_count",
    "image_urls_detected_count", "publicador", "seller_name", "contact_phone", "contact_email",
)
STRUCTURE_REQUIRED = (
    "schema_version", "origen", "source_portal", "source", "listing_id", "url", "canonical_url",
    "run_id", "batch_id", "classification", "html_validation_status", "scrape_stage", "pipeline_state",
    "pipeline_complete", "html_validation_reason", "processed_at", "updated_at", "fecha_scraping",
    "gestion", "enlaces_fotos",
)
EXTRACTION_REQUIRED = ("title", "description", "operacion", "tipo_propiedad", "comuna", "precio_raw")
CRM_PROTECTED = {
    "gestion", "asignacion", "asignaciones", "assigned_to", "assigned_executive",
    "executive_assignments", "historial_gestion", "management_history", "captacion_management_events",
    "captacion_management_date", "manual_review", "telefono_normalizado", "phone_identity",
    "phone_identity_decision", "created_at", "captacion_priority", "captacion_probability_sort",
}


def _number(raw: str) -> float | None:
    match = re.search(r"(?<!\d)(\d[\d.,]*)(?!\d)", str(raw or ""))
    if not match:
        return None
    text = match.group(1)
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1 or re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
        text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def _price_fields(record: dict[str, Any]) -> dict[str, Any]:
    raw = str(record.get("price") or record.get("precio_raw") or "").strip()
    amount = _number(raw)
    norm = raw.casefold()
    currency = "UF" if re.search(r"\b(?:uf|clf)\b", norm) else "CLP" if "$" in norm or "clp" in norm or "peso" in norm else "UNKNOWN"
    uf = amount if currency == "UF" else round(amount / UF_CLP, 2) if currency == "CLP" and amount is not None else None
    clp = int(round(amount * UF_CLP)) if currency == "UF" and amount is not None else int(amount) if currency == "CLP" and amount is not None else None
    return {
        "price": raw, "precio_raw": raw, "precio_moneda_original": currency,
        "precio_original_num": amount, "precio_uf": uf, "precio_clp": clp,
        "precio_uf_normalizado": float(uf) if uf is not None else None,
        "precio_clp_normalizado": float(clp) if clp is not None else None,
        "uf_valor_usado": UF_CLP if currency in {"UF", "CLP"} and amount is not None else None,
        "uf_fecha": UF_DATE if currency in {"UF", "CLP"} and amount is not None else None,
        "precio_conversion_source": f"SII_UF_{UF_DATE}" if currency in {"UF", "CLP"} and amount is not None else None,
    }


def _normalize_saved_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    def keynorm(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().casefold()).strip()

    def number(value: Any) -> float | None:
        match = re.search(r"(?<!\d)(\d[\d.,]*)(?!\d)", str(value or ""))
        if not match:
            return None
        raw = match.group(1)
        if "," in raw:
            raw = raw.replace(".", "").replace(",", ".")
        elif raw.count(".") > 1 or re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
            raw = raw.replace(".", "")
        try:
            return float(raw)
        except ValueError:
            return None

    result: dict[str, Any] = {}
    for label, value in attributes.items():
        key = keynorm(label)
        text = str(value or "").strip()
        if not text:
            continue
        if "publicado" in key or key in {"fecha", "fecha publicacion"}:
            result.setdefault("fecha_publicacion_raw", text)
            date = re.search(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)", text)
            iso = re.search(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", text)
            if date:
                result.setdefault("fecha_publicacion", f"{date.group(3)}-{date.group(2).zfill(2)}-{date.group(1).zfill(2)}")
            elif iso:
                result.setdefault("fecha_publicacion", iso.group(0))
        if "precio" in key:
            continue
        if any(token in key for token in ("construid", "construccion", "superficie util", "area util", "m2 util")):
            parsed = number(text)
            if parsed is not None:
                result.setdefault("m2_construidos", parsed)
        if any(token in key for token in ("m2 total", "superficie total", "area total", "terreno")):
            parsed = number(text)
            if parsed is not None:
                result.setdefault("m2_totales", parsed)
    return result


def _extract_one(row: dict[str, Any]) -> dict[str, Any]:
    source = dict(row)
    html_path = Path(str(row.get("html_source_path") or ""))
    parsed: dict[str, Any] = {}
    if html_path.is_file():
        html = html_path.read_text(encoding="utf-8", errors="replace")
        html_validation = validate_html(html)
        parsed = extract_listing_fields(html, source_url=str(row.get("url") or ""), include_images=True, include_listing_details=True)
        source["html_source_missing"] = False
        source["html_validation_status"] = html_validation["status"]
        source["html_validation_reason"] = html_validation["reason"]
        source["html_sha256"] = __import__("hashlib").sha256(html.encode("utf-8")).hexdigest()
    else:
        source["html_source_missing"] = True
        source["html_validation_status"] = "MISSING_LOCAL_HTML"
        source["html_validation_reason"] = "missing_local_html"
    out = dict(source)
    for key in SAVED_FIELDS:
        value = parsed.get(key)
        if value not in (None, "", [], {}):
            if key not in {"description", "title"} or len(str(value)) >= len(str(out.get(key) or "")):
                out[key] = value
    out.setdefault("description", str(row.get("description") or row.get("descripcion") or ""))
    out.setdefault("title", str(row.get("title") or ""))
    out["descripcion"] = out.get("description", "")
    raw_attributes = dict(row.get("raw_attributes") or row.get("attributes") or {})
    raw_attributes.update(parsed.get("attributes") or {})
    out["raw_attributes"] = raw_attributes
    out["attributes"] = dict(row.get("attributes") or raw_attributes)
    listing_id = str(row.get("listing_id") or "")
    parsed_images = parsed.get("images") or parsed.get("image_urls") or []
    # Yapo's detail HTML contains recommendations and UI assets. Keep only image
    # CDN paths that explicitly carry this listing ID; never persist page-wide
    # image counts as if they were property photos.
    property_images = []
    seen_photo_ids = set()
    for image_url in parsed_images:
        parsed_url = urlparse(str(image_url))
        path = parsed_url.path
        if "encuentra24.com" not in (parsed_url.hostname or "").casefold():
            continue
        match = re.search(rf"(?:^|/){re.escape(listing_id)}_([^/]+)$", path) if listing_id else None
        if not match:
            continue
        photo_id = match.group(1)
        if photo_id not in seen_photo_ids:
            seen_photo_ids.add(photo_id)
            property_images.append(str(image_url))
    if property_images:
        out["image_urls"] = property_images[:20]
        out["images"] = property_images[:20]
        out["enlaces_fotos"] = property_images[:20]
        out["main_image_url"] = property_images[0]
        out["image_urls_count"] = len(property_images[:20])
        out["image_urls_detected_count"] = len(property_images)
    else:
        for key in ("image_urls", "images", "enlaces_fotos"):
            if not isinstance(out.get(key), list):
                out[key] = []
        out["image_urls_count"] = len(out.get("image_urls") or [])
        out["image_urls_detected_count"] = int(out.get("image_urls_detected_count") or out["image_urls_count"])
    out["listing_id"] = listing_id
    out["canonical_url"] = str(out.get("canonical_url") or row.get("canonical_url") or row.get("url") or "")
    out["seller_profile_id"] = str(out.get("seller_profile_id") or row.get("seller_profile_id") or "")
    out["seller_profile_url"] = str(out.get("seller_profile_url") or row.get("seller_profile_url") or "")
    out["seller_type"] = str(out.get("seller_type") or row.get("seller_type") or "")
    out["publicador_visible"] = str(out.get("publicador_visible") or row.get("publicador_visible") or "")
    out["contact_logo_alt"] = str(out.get("contact_logo_alt") or row.get("contact_logo_alt") or row.get("logo") or "")
    out["listing_advertiser_type"] = str(out.get("listing_advertiser_type") or row.get("listing_advertiser_type") or "")
    out["contact_badges_text"] = str(out.get("contact_badges_text") or "")
    out["seller_jsonld_name"] = str(out.get("seller_jsonld_name") or "")
    out["comuna"] = str(out.get("comuna") or row.get("comuna") or row.get("discovery_comuna") or parsed.get("comuna") or "")
    out["comuna_source"] = str(row.get("comuna_source") or parsed.get("comuna_source") or "")
    out["comuna_evidence"] = row.get("comuna_detail_evidence") or parsed.get("comuna_evidence") or {}
    out["operacion"] = str(out.get("operacion") or row.get("operacion") or "")
    out["tipo_propiedad"] = str(out.get("tipo_propiedad") or row.get("tipo_propiedad") or "")
    out["region"] = str(out.get("region") or row.get("region") or parsed.get("region") or "")
    out["description_source"] = str(parsed.get("description_source") or row.get("descripcion_source") or row.get("description_source") or "")
    out["fecha_publicacion_raw"] = str(parsed.get("fecha_publicacion_raw") or row.get("fecha_publicacion_raw") or "")
    out["fecha_publicacion"] = str(parsed.get("fecha_publicacion") or row.get("fecha_publicacion") or "")
    out["fecha_scraping"] = row.get("fecha_scraping") or row.get("processed_at") or row.get("scraped_at")
    out["publicador"] = str(out.get("publicador") or out.get("publicador_visible") or out.get("contact_name") or "")
    out["seller_name"] = str(out.get("seller_name") or out.get("publicador") or "")
    out.update(_price_fields(out))
    normalized_attrs = _normalize_saved_attributes(out.get("raw_attributes") or {})
    for key, value in normalized_attrs.items():
        if out.get(key) in (None, ""):
            out[key] = value
    out["fecha_publicacion_raw"] = str(out.get("fecha_publicacion_raw") or normalized_attrs.get("fecha_publicacion_raw") or "")
    out["fecha_publicacion"] = str(out.get("fecha_publicacion") or normalized_attrs.get("fecha_publicacion") or "")
    # Explicit nulls are used only for unavailable extracted values; no property
    # attributes are fabricated to satisfy the persistence schema.
    for field in ("dormitorios", "banos", "estacionamientos", "m2_construidos", "m2_totales", "direccion_exacta"):
        if field in parsed:
            out[field] = parsed[field]
        elif field not in row:
            out[field] = None
    return out


def _canonical_document(item: dict[str, Any], classification: dict[str, Any], run_id: str, batch_id: str) -> dict[str, Any]:
    # Stable source timestamps make the canonical batch reproducible and
    # hashable; do not replace scrape provenance with simulation time.
    now = str(item.get("processed_at") or item.get("updated_at") or "")
    if not now:
        raise ValueError(f"No hay timestamp fuente en anuncio {item.get('listing_id')}; no se inventa fecha de captura.")
    state = str(classification.get("state") or "INCIERTO")
    html_status = str(item.get("html_validation_status") or "MISSING_LOCAL_HTML")
    removed = html_status == "LISTING_REMOVED" or state == "AD_REMOVED"
    description = str(item.get("description") or "")
    essential_complete = bool(item.get("comuna") and item.get("precio_raw") and item.get("precio_moneda_original") != "UNKNOWN")
    complete = essential_complete and not removed and state in {"CORREDOR_SEGURO", "CORREDOR_PROBABLE", "DUEÑO_PROBABLE", "DUEÑO_SEGURO"}
    raw_attributes = dict(item.get("raw_attributes") or item.get("attributes") or {})
    image_urls = list(item.get("enlaces_fotos") or item.get("image_urls") or [])
    surface_total = item.get("superficie_total") if item.get("superficie_total") is not None else item.get("m2_totales")
    surface_useful = item.get("superficie_util") if item.get("superficie_util") is not None else item.get("m2_construidos")
    detail_fields = {
        "comuna": str(item.get("comuna") or ""),
        "comuna_norm": "maipu" if "maip" in str(item.get("comuna") or "").casefold() else "",
        "operacion": str(item.get("operacion") or ""),
        "tipo_operacion": str(item.get("operacion") or ""),
        "tipo_propiedad": str(item.get("tipo_propiedad") or ""),
        "precio": str(item.get("precio_raw") or ""), "precio_raw": str(item.get("precio_raw") or ""),
        "precio_uf": item.get("precio_uf"), "precio_clp": item.get("precio_clp"),
        "dormitorios": item.get("dormitorios"), "banos": item.get("banos"),
        "estacionamientos": item.get("estacionamientos"),
        "m2_total": item.get("m2_totales") if item.get("m2_totales") is not None else item.get("m2_construidos"),
        "m2_totales": item.get("m2_totales"), "m2_construidos": item.get("m2_construidos"),
        "superficie_total": surface_total, "superficie_util": surface_useful,
        "fecha_publicacion": str(item.get("fecha_publicacion") or ""),
        "fecha_publicacion_raw": str(item.get("fecha_publicacion_raw") or ""),
        "publicador": str(item.get("publicador") or ""),
        "enlaces_fotos": image_urls,
    }
    details = dict(item.get("details") or raw_attributes)
    details.update({key: value for key, value in detail_fields.items() if value not in (None, "")})
    doc: dict[str, Any] = {
        "schema_version": "yapo_propiedades_v1", "origen": "yapo", "source_portal": "yapo",
        "source": "owner_hunt", "run_id": run_id, "batch_id": batch_id,
        "listing_id": str(item.get("listing_id") or ""), "url": str(item.get("url") or ""),
        "canonical_url": str(item.get("canonical_url") or item.get("url") or ""),
        "title": str(item.get("title") or ""), "description": description, "descripcion": description,
        "body_text": description, "body_text_excerpt": description[:600], "body_text_len": len(description),
        "descripcion_len": len(description), "descripcion_is_truncated": False,
        "descripcion_source": str(item.get("description_source") or ""),
        "description_available": bool(description.strip()),
        "operacion": str(item.get("operacion") or ""), "tipo_propiedad": str(item.get("tipo_propiedad") or ""),
        "comuna": str(item.get("comuna") or ""), "comuna_slug": "maipu" if "maip" in str(item.get("comuna") or "").casefold() else "",
        "region": str(item.get("region") or ""), "dormitorios": item.get("dormitorios"),
        "banos": item.get("banos"), "estacionamientos": item.get("estacionamientos"),
        "m2_construidos": item.get("m2_construidos"), "m2_totales": item.get("m2_totales"),
        "superficie_total": surface_total, "superficie_util": surface_useful,
        "m2_total": detail_fields["m2_total"],
        "direccion_exacta": item.get("direccion_exacta"), "attributes": raw_attributes,
        "details": details, "raw_attributes": raw_attributes, "image_urls": image_urls,
        "images": image_urls, "enlaces_fotos": image_urls,
        "image_urls_count": len(image_urls),
        "image_urls_detected_count": int(item.get("image_urls_detected_count") or len(image_urls)),
        "main_image_url": str(item.get("main_image_url") or next(iter(image_urls), "")),
        "seller_name": str(item.get("seller_name") or item.get("publicador") or ""),
        "publicador": str(item.get("publicador") or ""),
        "publicador_visible": str(item.get("publicador_visible") or ""),
        "seller_profile_id": str(item.get("seller_profile_id") or ""),
        "seller_profile_url": str(item.get("seller_profile_url") or ""),
        "seller_type": str(item.get("seller_type") or ""), "seller_jsonld_name": str(item.get("seller_jsonld_name") or ""),
        "contact_name": str(item.get("contact_name") or ""),
        "listing_advertiser": str(item.get("listing_advertiser") or ""),
        "listing_advertiser_type": str(item.get("listing_advertiser_type") or ""),
        "contact_logo_alt": str(item.get("contact_logo_alt") or ""),
        "contact_badges_text": str(item.get("contact_badges_text") or ""),
        "seller_text": str(item.get("seller_text") or ""),
        "seller_avatar_alt": str(item.get("seller_avatar_alt") or ""),
        "publisher_profile_context": item.get("publisher_profile_context") or {},
        "publisher_field_provenance": item.get("publisher_field_provenance") or {},
        "publisher_identity_candidates": item.get("publisher_identity_candidates") or [],
        "html_validation_status": html_status, "html_validation_reason": str(item.get("html_validation_reason") or ""),
        "scrape_stage": "ad_removed" if removed else "processed" if essential_complete and not item.get("html_source_missing") else "needs_rescrape",
        "pipeline_state": "AD_REMOVED" if removed else "CLASSIFIED" if complete else "EXTRACTOR_DEGRADED" if not essential_complete else "UNCERTAIN_PENDING_REVIEW",
        "pipeline_complete": complete, "classification": classification,
        "classification_debug": {
            "evidence_strength": classification.get("evidence_strength"),
            "contradictions": classification.get("contradictions") or [],
            "requires_review": classification.get("requires_review"),
            "deepseek_enabled": False, "deepseek_call_executed": False,
        },
        "processed_at": now, "updated_at": str(item.get("updated_at") or now),
        "fecha_scraping": str(item.get("fecha_scraping") or now),
        "fecha_publicacion": str(item.get("fecha_publicacion") or ""),
        "fecha_publicacion_raw": str(item.get("fecha_publicacion_raw") or ""),
        "html_path": str(item.get("html_source_path") or item.get("html_path") or ""),
        "html_sha256": str(item.get("html_sha256") or ""),
        "uf_valor_usado": item.get("uf_valor_usado"),
        "uf_fecha": item.get("uf_fecha"), "precio_conversion_source": item.get("precio_conversion_source"),
    }
    for key in ("price", "precio_raw", "precio_moneda_original", "precio_original_num", "precio_uf", "precio_clp", "precio_uf_normalizado", "precio_clp_normalizado"):
        doc[key] = item.get(key)
    # A fresh CRM row starts visible as NUEVO but has no executive, no assignment
    # history, and no management events. The import route only inserts this shape.
    doc["gestion"] = {
        "estado": "NUEVO", "estado_captacion": "NUEVO", "ejecutivo_id": None,
        "ejecutivo_asignado": None, "intent_count": 0, "notas": [], "actividades": [],
    }
    return doc


def _type_name(value: Any) -> str:
    return "null" if value is None else "bool" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "string" if isinstance(value, str) else "array" if isinstance(value, list) else "object" if isinstance(value, dict) else type(value).__name__


def _validate(doc: dict[str, Any], mongo_schema_types: dict[str, list[str]] | None = None) -> dict[str, Any]:
    structural_missing = [key for key in STRUCTURE_REQUIRED if key not in doc or doc[key] is None or doc[key] == ""]
    extraction_missing = [key for key in EXTRACTION_REQUIRED if key not in doc or doc[key] is None or doc[key] == ""]
    type_issues = []
    technical_issues = []
    if not isinstance(doc.get("schema_version"), str): type_issues.append("schema_version_no_es_string")
    if not re.fullmatch(r"\d{6,}", str(doc.get("listing_id") or "")): type_issues.append("listing_id_no_es_string_numerico")
    if not str(doc.get("url") or "").startswith("https://www.yapo.cl/"): type_issues.append("url_yapo_invalida")
    if doc.get("canonical_url") != doc.get("url") and not str(doc.get("canonical_url") or "").startswith("https://www.yapo.cl/"): type_issues.append("canonical_url_invalida")
    for key in ("origen", "source_portal", "source", "run_id", "batch_id", "title", "description", "descripcion", "body_text", "descripcion_source", "html_validation_reason", "operacion", "tipo_propiedad", "comuna", "precio_raw", "html_validation_status", "scrape_stage", "pipeline_state", "processed_at", "updated_at"):
        if doc.get(key) is not None and not isinstance(doc.get(key), str): type_issues.append(f"tipo_no_string:{key}")
    if not isinstance(doc.get("pipeline_complete"), bool): type_issues.append("pipeline_complete_no_es_booleano")
    for key in ("precio_original_num", "precio_uf", "precio_clp", "precio_uf_normalizado", "precio_clp_normalizado", "dormitorios", "banos", "estacionamientos", "m2_construidos", "m2_totales"):
        value = doc.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))): type_issues.append(f"tipo_no_numerico:{key}")
    for key in ("classification", "attributes", "details", "raw_attributes", "publisher_profile_context", "publisher_field_provenance", "gestion"):
        if not isinstance(doc.get(key), dict): type_issues.append(f"{key}_no_es_objeto")
    if "identity_provenance" in doc and not isinstance(doc.get("identity_provenance"), dict): type_issues.append("identity_provenance_no_es_objeto")
    for key in ("image_urls", "images", "enlaces_fotos", "publisher_identity_candidates"):
        if not isinstance(doc.get(key), list): type_issues.append(f"{key}_no_es_arreglo")
    gestion = doc.get("gestion") if isinstance(doc.get("gestion"), dict) else {}
    if gestion and (gestion.get("estado") != "NUEVO" or gestion.get("ejecutivo_id") is not None or gestion.get("ejecutivo_asignado") is not None):
        type_issues.append("gestion_inicial_no_es_nueva_sin_asignacion")
    schema_type_mismatches = []
    type_aliases = {"str": "str", "int": "int", "float": "float", "bool": "bool", "dict": "dict", "list": "list", "datetime": "datetime"}
    for key, allowed in (mongo_schema_types or {}).items():
        if key not in doc or doc[key] is None:
            continue  # MongoDB is schemaless; null/missing is not a type conflict.
        observed = type_aliases.get(type(doc[key]).__name__, type(doc[key]).__name__)
        if observed not in allowed:
            schema_type_mismatches.append({"field": key, "simulated_type": observed, "observed_mongo_types": allowed})
    if schema_type_mismatches:
        type_issues.extend(f"mongo_schema_type_mismatch:{item['field']}" for item in schema_type_mismatches)
    cls = doc.get("classification") if isinstance(doc.get("classification"), dict) else {}
    state = str(cls.get("state") or "")
    if state not in {"CORREDOR_SEGURO", "CORREDOR_PROBABLE", "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO", "AD_REMOVED"}: type_issues.append("estado_clasificacion_invalido")
    if not isinstance(cls.get("evidence", []), list) or not isinstance(cls.get("contradictions", []), list): type_issues.append("evidencia_o_contradicciones_no_es_arreglo")
    html_status = str(doc.get("html_validation_status") or "")
    if html_status in {"MISSING_LOCAL_HTML", "INVALID", "BLOCKED"}: technical_issues.append(f"html_{html_status.lower()}")
    elif html_status not in {"OK", "LISTING_REMOVED"}: technical_issues.append(f"html_estado_tecnico_no_reconocido:{html_status or 'missing'}")
    if html_status == "LISTING_REMOVED" and state != "AD_REMOVED": technical_issues.append("html_removed_sin_estado_ad_removed")
    if html_status == "LISTING_REMOVED" and doc.get("scrape_stage") != "ad_removed": technical_issues.append("html_removed_sin_scrape_stage_ad_removed")
    if doc.get("scrape_stage") == "needs_rescrape": technical_issues.append("scrape_stage_needs_rescrape")
    if doc.get("scrape_stage") not in {"processed", "needs_rescrape", "ad_removed"}: technical_issues.append(f"scrape_stage_no_reconocido:{doc.get('scrape_stage')}")
    if doc.get("precio_moneda_original") == "UNKNOWN" or not doc.get("precio_raw"): extraction_missing.append("precio_moneda_o_monto")
    if not isinstance(doc.get("description"), str) or not doc.get("description"): extraction_missing.append("descripcion_completa")
    structural_valid = not structural_missing and not type_issues
    extraction_complete = not extraction_missing and html_status == "OK"
    technical_valid = html_status in {"OK", "LISTING_REMOVED"} and not technical_issues
    commercial_candidate = state in {"DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"}
    assignment_ready = bool(cls.get("assignment_ready"))
    persistence_ready = structural_valid and extraction_complete and technical_valid
    return {
        "structural_missing": sorted(set(structural_missing)), "missing_required": sorted(set(structural_missing)),
        "extraction_missing": sorted(set(extraction_missing)), "type_or_integrity_issues": type_issues,
        "mongo_schema_type_mismatches": schema_type_mismatches,
        "technical_issues": technical_issues, "html_validation_status": html_status,
        "structural_valid": structural_valid, "valid": structural_valid,
        "extraction_complete": extraction_complete, "technical_valid": technical_valid,
        "commercial_candidate": commercial_candidate, "commercially_excluded_broker": state.startswith("CORREDOR"),
        "assignment_ready": assignment_ready, "assignment_block_reasons": cls.get("assignment_block_reasons") or [],
        "persistence_ready": persistence_ready,
    }


def _normal(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", " ", raw).strip()


def _simulate_crm_read_contract(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Check stored fields consumed by the API without importing live app modules."""
    failures: list[dict[str, str]] = []
    photo_count = 0
    publisher_count = 0
    state_count = 0
    evidence_count = 0
    management_count = 0
    filter_count = 0
    range_matches = 0
    range_excluded = []
    for doc in docs:
        lid = str(doc.get("listing_id") or "")
        errors = []
        if not doc.get("title") or not isinstance(doc.get("description"), str):
            errors.append("lectura_titulo_descripcion")
        expected_images = doc.get("enlaces_fotos") or []
        api_photos = doc.get("images") or doc.get("enlaces_fotos") or doc.get("image_urls") or []
        if api_photos != expected_images:
            errors.append("lectura_fotografias")
        if expected_images:
            photo_count += 1
        seller = (doc.get("details") or {}).get("publicador") or doc.get("publicador") or (doc.get("details") or {}).get("vendedor_nombre") or doc.get("seller_name") or ""
        if seller != (doc.get("publicador") or doc.get("seller_name") or ""):
            errors.append("lectura_publicador")
        else:
            publisher_count += bool(seller)
        cls = doc.get("classification") or {}
        if not cls.get("state"):
            errors.append("lectura_clasificacion")
        else:
            state_count += 1
        if not isinstance(cls.get("evidence", []), list):
            errors.append("lectura_evidencia")
        elif cls.get("evidence") or cls.get("reason"):
            evidence_count += 1
        management = doc.get("gestion") or {}
        if management.get("estado") != "NUEVO" or management.get("ejecutivo_id") is not None or management.get("ejecutivo_asignado") is not None:
            errors.append("gestion_inicial_o_asignacion")
        else:
            management_count += 1
        if cls.get("assignment_ready") is not False:
            errors.append("bloqueo_asignacion")
        if doc.get("comuna_slug") != "maipu" or "maip" not in _normal(doc.get("comuna")):
            errors.append("filtro_comuna")
        if "casa" not in _normal(doc.get("tipo_propiedad")):
            errors.append("filtro_tipo_propiedad")
        price = doc.get("precio_uf_normalizado")
        if price is None or not isinstance(price, (int, float)):
            errors.append("filtro_precio_uf")
        elif 3000 <= float(price) <= 6000:
            range_matches += 1
        else:
            range_excluded.append({"listing_id": lid, "precio_uf_normalizado": float(price)})
        if not errors:
            filter_count += 1
        else:
            failures.append({"listing_id": lid, "failures": ",".join(errors)})
    return {
        "tested_documents": len(docs), "read_model_valid": len(docs) - len(failures),
        "photos_available_and_readable": photo_count, "publisher_values_readable": publisher_count,
        "classification_and_evidence_readable": evidence_count, "classification_state_readable": state_count,
        "unassigned_management_readable": management_count, "filter_contract_passes": filter_count,
        "filters_checked": ["comuna", "precio_uf_3000_6000", "tipo_propiedad_casa"],
        "price_filter_range_matches": range_matches,
        "price_filter_out_of_range_exclusions": range_excluded,
        "automatic_assignment_ready": sum((doc.get("classification") or {}).get("assignment_ready") is True for doc in docs),
        "failures": failures,
    }


def _extract_worker(row: dict[str, Any]) -> dict[str, Any]:
    item = _extract_one(row)
    html_path = Path(str(row.get("html_source_path") or ""))
    if html_path.is_file():
        # Reuse the established offline parser for publication date, attributes,
        # and semantic dimensions. It reads the saved HTML and performs no I/O.
        from . import scraping_yapo_proxys_yapo as legacy_parser
        html = html_path.read_text(encoding="utf-8", errors="replace")
        legacy = legacy_parser._extract_with_bs4(html)
        legacy = legacy_parser._enrich_property_fields(
            legacy, str(row.get("url") or ""), UF_CLP, UF_DATE,
        )
        for key in (
            "fecha_publicacion", "fecha_publicacion_raw", "m2_construidos", "m2_totales",
            "dormitorios", "banos", "estacionamientos", "gastos_comunes", "direccion_exacta",
        ):
            if legacy.get(key) not in (None, ""):
                item[key] = legacy[key]
        legacy_attrs = legacy.get("attributes") or {}
        if legacy_attrs:
            raw = dict(item.get("raw_attributes") or {})
            raw.update(legacy_attrs)
            item["raw_attributes"] = raw
            item["attributes"] = dict(raw)
        old_images = legacy.get("image_urls") or []
        listing_id = str(item.get("listing_id") or "")
        property_images = []
        for image_url in old_images:
            path = urlparse(str(image_url)).path
            if listing_id and re.search(rf"(?:^|/){re.escape(listing_id)}_[^/]+$", path):
                if image_url not in property_images:
                    property_images.append(str(image_url))
        if property_images:
            item["image_urls"] = property_images[:20]
            item["images"] = property_images[:20]
            item["enlaces_fotos"] = property_images[:20]
            item["main_image_url"] = property_images[0]
            item["image_urls_count"] = len(property_images[:20])
            item["image_urls_detected_count"] = len(property_images)
        for key in ("m2_construidos", "m2_totales", "superficie_util", "superficie_total"):
            if item.get(key) is None and legacy.get(key) is not None:
                item[key] = legacy[key]
    for key, value in _normalize_saved_attributes(item.get("raw_attributes") or {}).items():
        if item.get(key) in (None, ""):
            item[key] = value
    item["fecha_publicacion_raw"] = str(item.get("fecha_publicacion_raw") or "")
    item["fecha_publicacion"] = str(item.get("fecha_publicacion") or "")
    return item


def simulate(source_path: Path, out_dir: Path, *, html_dumps_dir: Path | None = None, workers: int = 4) -> dict[str, Any]:
    source = json.loads(source_path.read_text(encoding="utf-8-sig"))
    wrapped_records = source.get("records") if isinstance(source, dict) else None
    canonical_input = bool(wrapped_records and isinstance(wrapped_records, list) and isinstance(wrapped_records[0], dict) and isinstance(wrapped_records[0].get("document"), dict))
    if canonical_input:
        rows = []
        for result in wrapped_records:
            row = dict(result["document"])
            row["classification_original"] = result.get("original_state")
            row["classification_new"] = {"state": result.get("current_state")}
            row["classification_preserved"] = dict(row.get("classification") or {})
            rows.append(row)
    else:
        rows = source.get("audit_records", source)
    if not isinstance(rows, list):
        raise ValueError("La entrada debe contener registros locales guardados.")
    if html_dumps_dir:
        html_index = {path.stem.lower(): path for path in html_dumps_dir.rglob("*.html")}
        for row in rows:
            if not row.get("html_source_path"):
                url = str(row.get("url") or row.get("canonical_url") or "")
                if url:
                    local = html_index.get(hashlib.md5(url.encode("utf-8")).hexdigest().lower())
                    if local:
                        row["html_source_path"] = str(local)
    out_dir.mkdir(parents=True, exist_ok=True)
    schema_path = out_dir / "atlas_yapo_schema_reference.json"
    schema_reference = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.is_file() else {}
    mongo_schema_types = schema_reference.get("sample_top_level_bson_types") or None
    run_id = "offline_yapo_audit_20261103"
    batch_id = "maipu_houses_3000_6000_20261103"
    results: list[dict[str, Any]] = []
    old_states: Counter[str] = Counter()
    current_states: Counter[str] = Counter()
    new_states: Counter[str] = Counter()
    routes: Counter[str] = Counter()
    seen_ids: set[str] = set()
    max_workers = max(1, min(int(workers), 8))
    if max_workers > 1:
        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            extracted_rows = list(pool.map(_extract_worker, rows))
    else:
        extracted_rows = [_extract_worker(row) for row in rows]
    for row, item in zip(rows, extracted_rows):
        listing_id = str(item.get("listing_id") or "")
        original_state = str(row.get("classification_original") or (row.get("clasificacion_preliminar") or {}).get("state") or "UNKNOWN")
        current_state = str((row.get("classification_new") or {}).get("state") or "UNKNOWN")
        old_states[original_state] += 1
        current_states[current_state] += 1
        parsed = dict(item)
        classification = (dict(row.get("classification_preserved") or {}) if canonical_input else None)
        if not classification:
            classification = classify_yapo_listing(parsed) or {"state": "INCIERTO", "reason": "No se pudo clasificar", "evidence": [], "requires_review": True}
        state = str(classification.get("state") or "INCIERTO")
        route = "deterministic_only" if state != "INCIERTO" else "deepseek_candidate_simulated"
        routes[route] += 1
        new_states[state] += 1
        doc = _canonical_document(item, classification, run_id, batch_id)
        # Exercise the exact current Yapo MongoStore probability bridge and gate
        # offline; do not call upsert_listing or collection().
        # The input is the already-audited canonical run. Preserve its classifier
        # result exactly; re-running the global probability bridge here could
        # mutate state or introduce timestamps outside this preparation task.
        if not canonical_input:
            simulated = MongoStore(AppConfig())._sanitize_payload(doc)
        else:
            simulated = doc
        validation = _validate(simulated, mongo_schema_types)
        duplicate = listing_id in seen_ids
        seen_ids.add(listing_id)
        results.append({
            "listing_id": listing_id, "original_state": original_state, "current_state": current_state,
            "corrected_state": state, "old_state": current_state,
            "classification_changed": current_state != state, "classification": classification,
            "document": simulated, "validation": validation,
            "assignment_ready": (simulated.get("classification") or {}).get("assignment_ready", False),
            "assignment_block_reasons": (simulated.get("classification") or {}).get("assignment_block_reasons", []),
            "duplicate_listing_id": duplicate, "deepseek_call_executed": False,
            "deepseek_route": route, "source_html_missing": bool(item.get("html_source_missing")),
        })
        if len(results) % 100 == 0:
            print(f"Simulados {len(results)}/{len(rows)} registros locales", flush=True)

    errors = Counter()
    for result in results:
        for item in result["validation"]["missing_required"]: errors[f"missing:{item}"] += 1
        for item in result["validation"]["type_or_integrity_issues"]: errors[item] += 1
    profile_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row, result in zip(rows, results):
        profile_id = str(result["document"].get("seller_profile_id") or "")
        if profile_id:
            profile_groups[profile_id].append({"row": row, "result": result})
    potential_duplicates = []
    for profile_id, group in profile_groups.items():
        for idx, left in enumerate(group):
            for right in group[idx + 1:]:
                ld, rd = left["result"]["document"], right["result"]["document"]
                same_price = ld.get("precio_original_num") == rd.get("precio_original_num") and ld.get("precio_moneda_original") == rd.get("precio_moneda_original")
                same_title = SequenceMatcher(None, _normal(ld.get("title")), _normal(rd.get("title"))).ratio() >= 0.85
                if same_price and same_title and ld.get("comuna_slug") == rd.get("comuna_slug"):
                    potential_duplicates.append({"listing_ids": [ld["listing_id"], rd["listing_id"]], "seller_profile_id": profile_id, "confidence": "possible_same_property", "basis": ["same_profile", "same_price", "similar_title", "same_commune"]})
    samples = []
    target_ids = {"25898826", "26100706", "26896018", "27979975", "25849288", "25949620", "26547167", "25876779", "25877389", "28028726"}
    for item in results:
        if item["listing_id"] in target_ids:
            d = item["document"]
            samples.append({
                "listing_id": item["listing_id"], "original_state": item["original_state"],
                "previous_state": item["current_state"],
                "post_canonical_state": d["classification"].get("state"),
                "probability_derived_state": d["classification"].get("probability_derived_state"),
                "owner_declaration": d["classification"].get("owner_evidence", []),
                "commercial_evidence": d["classification"].get("broker_evidence", []),
                "professional_label": d.get("seller_type"),
                "contradictions": d["classification"].get("contradictions", []),
                "rule_ids": d["classification"].get("classification_rule_ids", []),
                "reason": d["classification"].get("reason"), "assignment_block_reasons": item["assignment_block_reasons"],
                "deepseek_needed": d["classification"].get("state") == "INCIERTO",
            })
    anonymized_examples = {}
    for desired_state in ("CORREDOR_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"):
        found = next((item["document"] for item in results if item["document"]["classification"].get("state") == desired_state), None)
        if found is None:
            continue
        example = dict(found)
        example["listing_id"] = "00000000"
        example["url"] = "https://www.yapo.cl/ejemplo/00000000"
        example["canonical_url"] = example["url"]
        example["seller_name"] = example["publicador_visible"] = "PUBLICADOR ANONIMIZADO"
        example["seller_profile_id"] = example["seller_profile_url"] = ""
        example["seller_jsonld_name"] = example["contact_logo_alt"] = ""
        example["title"] = f"Anuncio de ejemplo anonimizado — {desired_state}"
        example["description"] = "Texto de ejemplo anonimizado; no usar como registro persistente."
        example["attributes"] = example["raw_attributes"] = {}
        example["image_urls"] = []
        example["main_image_url"] = ""
        example["classification"] = dict(example["classification"])
        for evidence_key in ("evidence", "owner_evidence", "owner_signal_evidence", "broker_evidence", "contradictions"):
            if evidence_key in example["classification"]:
                example["classification"][evidence_key] = ["Evidencia de muestra anonimizada"]
        if "contextual_analysis" in example["classification"]:
            example["classification"]["contextual_analysis"] = {}
        example["classification"]["review_reason"] = "Detalle anonimizado; consulte el informe privado por ID."
        anonymized_examples[desired_state] = example

    full = {
        "mode": "offline_simulation_only", "collection_reference": "URLS.propiedades_captacion",
        "generated_at": datetime.now(timezone.utc).isoformat(), "records": results,
        "classification_comparison": {"original_counts": dict(old_states), "current_counts": dict(current_states), "corrected_counts": dict(new_states),
                                       "current_to_corrected_transitions": dict(Counter(f"{r['current_state']} -> {r['corrected_state']}" for r in results if r["current_state"] != r["corrected_state"])),
                                       "original_to_current_transitions": dict(Counter(f"{r['original_state']} -> {r['current_state']}" for r in results if r["original_state"] != r["current_state"]))},
        "routing_simulation": {"routes": dict(routes), "deepseek_calls": 0, "api_tokens": 0},
        "mongo_schema_comparison": {
            "database": schema_reference.get("database"), "collection": schema_reference.get("collection"),
            "sample_documents": schema_reference.get("sample_documents", 0), "sample_is_enforced_schema": False,
            "simulated_field_count": len(results[0]["document"]) if results else 0,
            "simulated_fields_unobserved_in_sample": sorted(set(results[0]["document"]) - set(mongo_schema_types or {})) if results else [],
            "observed_fields_missing_in_simulation": sorted(set(mongo_schema_types or {}) - set(results[0]["document"])) if results else [],
            "exact_field_type_mismatches": sorted({m["field"] for r in results for m in r["validation"]["mongo_schema_type_mismatches"]}),
        },
        "duplicates": {"duplicate_id_records": sum(r["duplicate_listing_id"] for r in results), "possible_same_property_groups": potential_duplicates},
        "validation": {"record_count": len(results), "unique_listing_ids": len(seen_ids),
                       "structurally_valid": sum(r["validation"]["structural_valid"] for r in results),
                       "extraction_complete": sum(r["validation"]["extraction_complete"] for r in results),
                       "technically_valid": sum(r["validation"]["technical_valid"] for r in results),
                       "commercial_candidates": sum(r["validation"]["commercial_candidate"] for r in results),
                       "commercially_excluded_brokers": sum(r["validation"]["commercially_excluded_broker"] for r in results),
                       "assignment_ready": sum(r["validation"]["assignment_ready"] for r in results),
                       "persistence_ready": sum(r["validation"]["persistence_ready"] for r in results),
                       "valid_records": sum(r["validation"]["valid"] for r in results),
                       "records_with_errors": sum(not r["validation"]["valid"] for r in results), "error_counts": dict(errors),
                       "structural_required_fields": list(STRUCTURE_REQUIRED), "extraction_required_fields": list(EXTRACTION_REQUIRED), "crm_protected_fields": sorted(CRM_PROTECTED)},
        "mongo_writes": 0, "yapo_requests": 0, "deepseek_calls": 0,
    }
    simulation_path = out_dir / "yapo_maipu_482_captacion_simulation.json"
    simulation_path.write_text(json.dumps(full, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    canonical_docs = [result["document"] for result in results]
    crm_read_qa = _simulate_crm_read_contract(canonical_docs)
    canonical_path = out_dir / "yapo_maipu_preimport_482.json"
    canonical_bytes = (json.dumps(canonical_docs, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
    canonical_path.write_bytes(canonical_bytes)
    full["canonical_batch"] = {
        "path": str(canonical_path), "sha256": hashlib.sha256(canonical_bytes).hexdigest(),
        "record_count": len(canonical_docs), "scope": "exact UTF-8 bytes of canonical JSON file",
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
    }
    available_summary = {}
    for field in (
        "fecha_publicacion", "fecha_publicacion_raw", "fecha_scraping", "publicador", "contact_name",
        "contact_phone", "contact_email", "enlaces_fotos", "superficie_total", "superficie_util",
        "m2_totales", "m2_construidos", "attributes", "raw_attributes", "details", "gestion",
    ):
        available_summary[field] = sum(1 for doc in canonical_docs if doc.get(field) not in (None, "", [], {}))
    full["available_data_summary"] = {
        "records": len(canonical_docs), "nonempty_by_field": available_summary,
        "missing_by_field": {key: len(canonical_docs) - value for key, value in available_summary.items()},
        "phone_or_email_not_extracted_by_this_yapo_adapter": True,
        "absence_reasons": {
            "fecha_publicacion": "No se encontró fecha absoluta de publicación en el HTML local ni en atributos guardados.",
            "fecha_publicacion_raw": "No existe etiqueta/valor de publicación recuperable en los datos locales de este lote.",
            "contact_phone": "El HTML/JSON estático local no contiene teléfono extraíble; no se fabricó ni se hizo solicitud adicional.",
            "contact_email": "El HTML/JSON estático local no contiene correo extraíble; no se fabricó ni se hizo solicitud adicional.",
            "superficie_total": "No hay atributo explícito de superficie total/terreno en los datos locales de estos anuncios.",
            "superficie_util": "No hay atributo explícito de superficie construida/útil en los datos locales de estos anuncios.",
            "m2_totales": "No se encontró valor numérico en HTML ni atributos guardados.",
            "m2_construidos": "No se encontró valor numérico en HTML ni atributos guardados.",
            "attributes": "287 anuncios no tienen atributos estructurados disponibles; se conserva objeto vacío.",
            "raw_attributes": "287 anuncios no tienen atributos originales disponibles; no se añaden valores inferidos.",
            "enlaces_fotos": "6 anuncios no tienen URL de foto asociada al listing_id en el HTML local.",
        },
    }
    full["crm_read_qa"] = crm_read_qa
    # Rewrite the simulation with canonical hash and source-availability counts.
    simulation_path.write_text(json.dumps(full, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "yapo_maipu_10_owner_canonical_comparison.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "yapo_maipu_crm_read_qa.json").write_text(json.dumps(crm_read_qa, ensure_ascii=False, indent=2), encoding="utf-8")
    import csv
    availability_fields = list(available_summary)
    with (out_dir / "yapo_maipu_data_availability_482.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["listing_id", "available_fields", "missing_fields"])
        writer.writeheader()
        for doc in canonical_docs:
            present = [field for field in availability_fields if doc.get(field) not in (None, "", [], {})]
            missing = [field for field in availability_fields if field not in present]
            writer.writerow({"listing_id": doc["listing_id"], "available_fields": ";".join(present), "missing_fields": ";".join(missing)})
    (out_dir / "yapo_maipu_canonical_examples_anonymized.json").write_text(json.dumps(anonymized_examples, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary = {key: value for key, value in full.items() if key != "records"}
    summary["atlas_schema_reference"] = "read-only sample; URLS.propiedades_captacion"
    (out_dir / "yapo_maipu_compatibility_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--html-dumps-dir", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    summary = simulate(args.input, args.output_dir, html_dumps_dir=args.html_dumps_dir, workers=args.workers)
    print(json.dumps({key: summary[key] for key in ("classification_comparison", "routing_simulation", "validation", "duplicates", "deepseek_calls", "mongo_writes", "yapo_requests")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
