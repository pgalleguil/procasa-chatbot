"""Offline Yapo-to-captacion compatibility audit for saved listings.

Reads local JSON/HTML only. It never connects to MongoDB or calls DeepSeek.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

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
    "attributes", "image_urls", "main_image_url", "canonical_url",
    "publisher_field_provenance", "publisher_identity_candidates", "publisher_profile_context",
)
STRUCTURE_REQUIRED = (
    "schema_version", "origen", "source_portal", "source", "listing_id", "url", "canonical_url",
    "run_id", "batch_id", "classification", "html_validation_status", "scrape_stage", "pipeline_state",
    "pipeline_complete", "html_validation_reason", "processed_at", "updated_at",
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
    out.setdefault("attributes", parsed.get("attributes") or row.get("attributes") or {})
    out.setdefault("image_urls", parsed.get("image_urls") or row.get("image_urls") or [])
    out["listing_id"] = str(row.get("listing_id") or "")
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
    out.update(_price_fields(out))
    # Explicit nulls are used only for unavailable extracted values; no property
    # attributes are fabricated to satisfy the persistence schema.
    for field in ("dormitorios", "banos", "estacionamientos", "m2_construidos", "m2_totales", "direccion_exacta"):
        if field in parsed:
            out[field] = parsed[field]
        elif field not in row:
            out[field] = None
    return out


def _canonical_document(item: dict[str, Any], classification: dict[str, Any], run_id: str, batch_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    state = str(classification.get("state") or "INCIERTO")
    html_status = str(item.get("html_validation_status") or "MISSING_LOCAL_HTML")
    removed = html_status == "LISTING_REMOVED" or state == "AD_REMOVED"
    description = str(item.get("description") or "")
    essential_complete = bool(item.get("comuna") and item.get("precio_raw") and item.get("precio_moneda_original") != "UNKNOWN")
    complete = essential_complete and not removed and state in {"CORREDOR_SEGURO", "CORREDOR_PROBABLE", "DUEÑO_PROBABLE", "DUEÑO_SEGURO"}
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
        "direccion_exacta": item.get("direccion_exacta"), "attributes": item.get("attributes") or {},
        "details": item.get("attributes") or {}, "raw_attributes": item.get("attributes") or {}, "image_urls": item.get("image_urls") or [],
        "images": item.get("images") or item.get("image_urls") or [],
        "image_urls_count": len(item.get("image_urls") or []),
        "main_image_url": str(item.get("main_image_url") or next(iter(item.get("image_urls") or []), "")),
        "seller_name": str(item.get("publicador_visible") or ""), "publicador_visible": str(item.get("publicador_visible") or ""),
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
        "processed_at": now, "updated_at": now, "uf_valor_usado": item.get("uf_valor_usado"),
        "uf_fecha": item.get("uf_fecha"), "precio_conversion_source": item.get("precio_conversion_source"),
    }
    for key in ("price", "precio_raw", "precio_moneda_original", "precio_original_num", "precio_uf", "precio_clp", "precio_uf_normalizado", "precio_clp_normalizado"):
        doc[key] = item.get(key)
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
    for key in ("classification", "attributes", "details", "raw_attributes", "publisher_profile_context", "publisher_field_provenance"):
        if not isinstance(doc.get(key), dict): type_issues.append(f"{key}_no_es_objeto")
    if "identity_provenance" in doc and not isinstance(doc.get("identity_provenance"), dict): type_issues.append("identity_provenance_no_es_objeto")
    for key in ("image_urls", "images", "publisher_identity_candidates"):
        if not isinstance(doc.get(key), list): type_issues.append(f"{key}_no_es_arreglo")
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


def simulate(source_path: Path, out_dir: Path) -> dict[str, Any]:
    source = json.loads(source_path.read_text(encoding="utf-8-sig"))
    rows = source.get("audit_records", source)
    out_dir.mkdir(parents=True, exist_ok=True)
    schema_path = out_dir / "atlas_yapo_schema_reference.json"
    schema_reference = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.is_file() else {}
    mongo_schema_types = schema_reference.get("sample_top_level_bson_types") or None
    store = MongoStore(AppConfig())  # no connect() call; sanitizer only
    run_id = "offline_yapo_audit_20261103"
    batch_id = "maipu_houses_3000_6000_20261103"
    results: list[dict[str, Any]] = []
    old_states: Counter[str] = Counter()
    current_states: Counter[str] = Counter()
    new_states: Counter[str] = Counter()
    routes: Counter[str] = Counter()
    seen_ids: set[str] = set()
    for row in rows:
        item = _extract_one(row)
        listing_id = str(item.get("listing_id") or "")
        original_state = str(row.get("classification_original") or (row.get("clasificacion_preliminar") or {}).get("state") or "UNKNOWN")
        current_state = str((row.get("classification_new") or {}).get("state") or "UNKNOWN")
        old_states[original_state] += 1
        current_states[current_state] += 1
        parsed = dict(item)
        classification = classify_yapo_listing(parsed) or {"state": "INCIERTO", "reason": "No se pudo clasificar", "evidence": [], "requires_review": True}
        state = str(classification.get("state") or "INCIERTO")
        route = "deterministic_only" if state != "INCIERTO" else "deepseek_candidate_simulated"
        routes[route] += 1
        new_states[state] += 1
        doc = _canonical_document(item, classification, run_id, batch_id)
        # Exercise the exact current Yapo MongoStore probability bridge and gate
        # offline; do not call upsert_listing or collection().
        simulated = store._sanitize_payload(doc)
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
    (out_dir / "yapo_maipu_482_captacion_simulation.json").write_text(json.dumps(full, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "yapo_maipu_10_owner_canonical_comparison.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "yapo_maipu_canonical_examples_anonymized.json").write_text(json.dumps(anonymized_examples, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary = {key: value for key, value in full.items() if key != "records"}
    summary["atlas_schema_reference"] = "read-only sample; URLS.propiedades_captacion"
    (out_dir / "yapo_maipu_compatibility_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = simulate(args.input, args.output_dir)
    print(json.dumps({key: summary[key] for key in ("classification_comparison", "routing_simulation", "validation", "duplicates", "deepseek_calls", "mongo_writes", "yapo_requests")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
