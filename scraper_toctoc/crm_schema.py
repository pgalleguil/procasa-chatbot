from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

try:
    from comuna_utils import normalize_commune_slug, normalize_toctoc_commune
except ImportError:  # ejecución directa desde scraper_toctoc/
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from comuna_utils import normalize_commune_slug, normalize_toctoc_commune

try:
    from owner_probability import expected_state_for_probability
except ImportError:  # ejecución directa desde scraper_toctoc/
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from owner_probability import expected_state_for_probability

def _utcnow() -> str: return datetime.now(timezone.utc).isoformat()


def parse_chilean_integer(text: str) -> int | None:
    if not text: return None
    try: return int(text.replace(".", "").strip())
    except (ValueError, TypeError): return None


def parse_chilean_decimal(text: str) -> float | None:
    if not text: return None
    try: return float(text.replace(".", "").replace(",", ".").strip())
    except (ValueError, TypeError): return None


def _parse_int(val: Any) -> int | None:
    if val is None or val == "" or val == "N/A": return None
    if isinstance(val, int): return val
    if isinstance(val, float): return int(val)
    s = re.sub(r"[^\d]", "", str(val).split(" a ")[0].strip())
    try: return int(s) if s else None
    except: return None


def _parse_float(val: Any) -> float | None:
    if val is None or val == "" or val == "N/A": return None
    if isinstance(val, (int, float)): return float(val)
    s = str(val).replace(",", ".").strip()
    s = re.sub(r"[^\d.]", "", s)
    try: return float(s) if s else None
    except: return None


def extract_price_uf(text: str) -> tuple[float | None, str]:
    m = re.search(r'\bUF\s*([\d.]+(?:,\d+)?)', text, re.I)
    if m:
        raw = m.group(1)
        val = parse_chilean_decimal(raw)
        return val, f"UF {raw}"
    return None, ""


def extract_price_clp(text: str) -> tuple[int | None, str]:
    m = re.search(r'\$\s*([\d.]+)', text)
    if m:
        raw = m.group(1)
        val = parse_chilean_integer(raw)
        return val, f"${raw}"
    return None, ""


DEEPSEEK_STATUS_LEGACY = "LEGACY_UNKNOWN"


def normalize_classification(raw: dict[str, Any]) -> dict[str, Any]:
    raw_state = raw.get("state", "INCIERTO")
    state = raw_state
    owner_probability = raw.get("owner_probability")
    try:
        if owner_probability is not None and float(owner_probability) > 1:
            owner_probability = float(owner_probability) / 100.0
        elif owner_probability is not None:
            owner_probability = float(owner_probability)
    except (TypeError, ValueError):
        owner_probability = None
    decision_source = raw.get("decision_source") or raw.get("source", "rules")
    hard_veto = str(raw.get("hard_veto") or raw.get("professional_hard_veto") or "").upper()
    if not hard_veto and str(raw_state).upper() == "CORREDOR_SEGURO" and str(decision_source).lower() in {
        "structural_rules", "rules_json", "structural_professional_rule"
    } and raw.get("strong_signal_found", True) is not False:
        hard_veto = "PROFESSIONAL"

    if owner_probability is not None and hard_veto != "PROFESSIONAL":
        state = expected_state_for_probability(owner_probability)
    if state == "INCONCLUSIVE": state = "INCIERTO"
    if state not in ("CORREDOR_SEGURO", "CORREDOR_PROBABLE", "DUEÑO_PROBABLE", "DUEÑO_SEGURO", "INCIERTO", "AD_REMOVED"):
        state = "INCIERTO"
    rule_state = raw.get("rule_state", "INCONCLUSIVE")
    if rule_state not in ("CORREDOR_SEGURO", "CORREDOR_PROBABLE", "DUEÑO_PROBABLE", "DUEÑO_SEGURO", "INCIERTO", "INCONCLUSIVE", "AD_REMOVED"):
        rule_state = "INCONCLUSIVE"
    confidence = raw.get("confidence", 0.0)
    try: confidence = float(confidence)
    except: confidence = 0.0
    if owner_probability is not None:
        confidence = round(owner_probability, 2)
    rule_confidence = raw.get("rule_confidence", raw.get("confidence"))
    try: rule_confidence = float(rule_confidence)
    except: rule_confidence = None
    evidence = [str(e) for e in (raw.get("evidence", []) or raw.get("signals", [])) if str(e).strip()]
    
    # Preserve DeepSeek tracing fields (or mark legacy)
    deepseek_status = raw.get("deepseek_status") or DEEPSEEK_STATUS_LEGACY
    state_source = decision_source
    
    # Extract DeepSeek usage from raw if available (support both deepseek_raw and legacy raw)
    ds_raw = raw.get("deepseek_raw", None)
    if not isinstance(ds_raw, dict):
        ds_raw = raw.get("raw", {}) if isinstance(raw.get("raw"), dict) else {}
    usage = ds_raw.get("usage", {}) or {}
    finish_reason = ""
    model = ""
    provider = ""
    if ds_raw:
        try:
            choices = ds_raw.get("choices", [{}])
            if choices:
                finish_reason = choices[0].get("finish_reason", "") or ""
                msg = choices[0].get("message", {})
                if not finish_reason:
                    finish_reason = msg.get("finish_reason", "") or ""
        except: pass
        try:
            model = ds_raw.get("model", "") or ""
            provider = ds_raw.get("provider", "") or ""
        except: pass
    
    normalized = {
        "state": state, "final_state": state, "confidence": confidence, "score": confidence,
        "canonical_confidence": confidence, "owner_probability": owner_probability,
        "rule_confidence": rule_confidence,
        "status": raw.get("status", ""),
        "rule_state": rule_state, "signals": raw.get("signals", {}),
        "evidence": evidence, "reason": str(raw.get("reason", "")),
        "decision_source": decision_source, "decision_pattern": raw.get("decision_pattern", ""),
        "hard_veto": hard_veto,
        "state_source": state_source,
        "deepseek_status": deepseek_status,
        "deepseek_state": "",
        "deepseek_confidence": 0.0,
        "deepseek_reason": str(raw.get("deepseek_reason", "")),
        "deepseek_evidence": [],
        "deepseek_model": model or ("deepseek-v4-flash" if raw.get("source") == "deepseek" or deepseek_status != DEEPSEEK_STATUS_LEGACY else ""),
        "deepseek_finish_reason": finish_reason,
        "deepseek_prompt_tokens": usage.get("prompt_tokens", 0) if usage else 0,
        "deepseek_completion_tokens": usage.get("completion_tokens", 0) if usage else 0,
        "deepseek_reasoning_tokens": usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0) if usage else 0,
        "deepseek_provider": provider,
        "version": "toctoc-v1",
        "ai_used": raw.get("source") == "deepseek",
        "ai_model": model or ("deepseek-v4-flash" if raw.get("source") == "deepseek" else ""),
        "ai_owner_score": 0.0, "ai_broker_score": 0.0,
        "ai_reason": str(raw.get("reason", "")),
        "ai_disabled_reason": str(raw.get("ai_not_used_reason", "")),
        "rules_version": str(raw.get("rules_version") or "toctoc-owner-rules-v2"),
        "prompt_version": str(raw.get("prompt_version") or "toctoc-deepseek-owner-v2"),
        "analysis_at": str(raw.get("analysis_at") or ""),
        "deepseek_payload": raw.get("deepseek_payload") or {},
        "deepseek_message_content": str(raw.get("deepseek_message_content") or ""),
        "deepseek_reasoning_content": str(raw.get("deepseek_reasoning_content") or ""),
        "deepseek_raw": ds_raw,
        "trace": raw.get("trace") or {},
    }
    normalized["assignment_ready"] = (
        state in {"DUEÑO_PROBABLE", "DUEÑO_SEGURO"}
        and not raw.get("manual_review_required")
        and ((state_source in ("structural_rules", "rules_json") and rule_state != "INCONCLUSIVE")
             or (state_source == "deepseek" and deepseek_status == "VALID" and bool(ds_raw)))
    )
    normalized["assignment_gate_version"] = "assignment-gate-v2"
    return normalized


COMUNA_TO_REGION = {
    "la florida": "Metropolitana", "santiago": "Metropolitana", "las condes": "Metropolitana",
    "providencia": "Metropolitana", "nunoa": "Metropolitana", "vitacura": "Metropolitana",
    "lo barnechea": "Metropolitana", "lo-barnechea": "Metropolitana", "maipu": "Metropolitana",
    "puente alto": "Metropolitana", "la reina": "Metropolitana", "penalolen": "Metropolitana",
    "macul": "Metropolitana", "san miguel": "Metropolitana", "recoleta": "Metropolitana",
    "independencia": "Metropolitana", "conchali": "Metropolitana", "renca": "Metropolitana",
    "quilicura": "Metropolitana", "el bosque": "Metropolitana", "cerro navia": "Metropolitana",
    "la cisterna": "Metropolitana", "la granja": "Metropolitana", "lo espejo": "Metropolitana",
    "lo prado": "Metropolitana", "padre hurtado": "Metropolitana", "peñaflor": "Metropolitana",
    "pirque": "Metropolitana", "san bernardo": "Metropolitana", "san joaquin": "Metropolitana",
    "san ramon": "Metropolitana", "talagante": "Metropolitana", "til til": "Metropolitana",
    "colina": "Metropolitana", "lampa": "Metropolitana", "calera de tango": "Metropolitana",
    "valparaiso": "Valparaiso", "vina del mar": "Valparaiso", "viña del mar": "Valparaiso",
    "quilpue": "Valparaiso", "quilpué": "Valparaiso",
    "coquimbo": "Coquimbo", "la serena": "Coquimbo",
    "antofagasta": "Antofagasta",
    "concepcion": "Biobio", "concepción": "Biobio",
    "temuco": "Araucania", "villarrica": "Araucania", "pucon": "Araucania", "pucón": "Araucania",
    "puerto varas": "Los Lagos", "puerto montt": "Los Lagos",
}


def build_crm_document(raw: dict[str, Any], uf_valor_clp: float = 40844.79, uf_fecha: str = "2026-07-14") -> dict[str, Any]:
    origen = "toctoc"
    listing_id = str(raw.get("listing_id") or "").strip()
    url = str(raw.get("url") or raw.get("source_url") or "")
    title = str(raw.get("title") or raw.get("titulo") or "")
    operacion = str(raw.get("operacion") or "").lower()
    tipo_prop = str(raw.get("tipo_propiedad") or "").lower()
    comuna = str(raw.get("comuna") or "")
    region = str(raw.get("region") or "")
    if not region and comuna.lower() in COMUNA_TO_REGION:
        region = COMUNA_TO_REGION[comuna.lower()]
    seller_name = str(raw.get("publicador_visible") or raw.get("seller_name") or raw.get("contact_name") or "")
    seller_text = str(raw.get("seller_text") or raw.get("seller_type_evidence") or seller_name or "")
    seller_avatar = str(raw.get("contact_logo_alt") or raw.get("seller_avatar_alt") or "")
    description = str(raw.get("description") or raw.get("descripcion") or "")
    images = raw.get("images") or raw.get("image_urls") or []
    if isinstance(images, str): images = [images]
    images = [img for img in images if not any(pat in img.lower() for pat in [
        "thumbs_up", "check.png", "1x1", "banner", "logo", "icon",
        "sprite", "avatar", "placeholder", "loading", "social",
        "favicon", "marker", "pin", ".svg", ".gif",
        "facebook.com/tr", "google-analytics", "googletagmanager",
        "pixel", "tracking", "beacon", "doubleclick",
    ])]

    seller_type = str(raw.get("seller_type") or "DESCONOCIDO")
    if seller_type not in ("PARTICULAR", "EMPRESA", "DESCONOCIDO"): seller_type = "DESCONOCIDO"

    # --- PRICE: separate UF and CLP, never concatenate ---
    price_raw = str(raw.get("price") or raw.get("precio_raw") or "")
    combined_price_text = f"{raw.get('price_uf','')} {raw.get('price_clp','')} {price_raw}"
    direct_uf = _parse_float(raw.get("precio_uf") or raw.get("price_uf"))
    direct_clp = _parse_int(raw.get("precio_clp") or raw.get("price_clp"))
    parsed_uf, parsed_uf_raw = extract_price_uf(combined_price_text)
    parsed_clp, parsed_clp_raw = extract_price_clp(combined_price_text)
    price_uf_val = direct_uf or parsed_uf
    price_clp_val = direct_clp or parsed_clp
    price_uf_raw = f"UF {price_uf_val}" if direct_uf else parsed_uf_raw
    price_clp_raw = f"${price_clp_val}" if direct_clp else parsed_clp_raw

    precio_moneda_original = "UF" if price_uf_val else "CLP"
    precio_original_num = price_uf_val if price_uf_val else price_clp_val

    price_validation = "OK"
    if price_uf_val is not None and price_uf_val > 1_000_000:
        price_validation = "INVALID_UF_RANGE"; price_uf_val = None
    if price_clp_val is not None and price_clp_val > 100_000_000_000:
        price_validation = "INVALID_CLP_RANGE"; price_clp_val = None

    had_original_uf = bool(price_uf_val)
    had_original_clp = bool(price_clp_val)
    if price_uf_val and not price_clp_val:
        price_clp_val = round(price_uf_val * uf_valor_clp)
        price_clp_raw = f"${price_clp_val:,.0f}"
    if price_clp_val and not price_uf_val:
        price_uf_val = round(price_clp_val / uf_valor_clp, 2)
        price_uf_raw = f"UF {price_uf_val:,.2f}"

    price_display = f"{price_uf_raw} / {price_clp_raw}".strip(" / ").strip()

    # --- DORM/BANOS: null for ranges ---
    def _parse_range(val_str, parse_fn):
        if not val_str or val_str == "N/A": return None, None, None
        s = str(val_str)
        parts = re.split(r'\s+a\s+', s, maxsplit=1)
        lo = parse_fn(parts[0]) if parts else None
        hi = parse_fn(parts[1]) if len(parts) > 1 and parts[1] else None
        return lo, hi, s

    dorm_lo, dorm_hi, dorm_raw = _parse_range(
        raw.get("dormitorios") or raw.get("dormitorios_raw") or "",
        lambda x: int(re.sub(r"[^\d]", "", x)) if re.sub(r"[^\d]", "", x) else None
    )
    ban_lo, ban_hi, ban_raw = _parse_range(
        raw.get("banos") or raw.get("banos_raw") or "",
        lambda x: int(re.sub(r"[^\d]", "", x)) if re.sub(r"[^\d]", "", x) else None
    )
    is_dorm_range = dorm_lo is not None and dorm_hi is not None and dorm_lo != dorm_hi
    is_ban_range = ban_lo is not None and ban_hi is not None and ban_lo != ban_hi

    sup_lo, sup_hi, sup_raw = _parse_range(
        raw.get("superficie_total") or "",
        lambda x: parse_chilean_decimal(re.sub(r"m[²2].*", "", x).strip())
    )
    m2c_lo, m2c_hi, m2c_raw = _parse_range(
        raw.get("superficie_util") or "",
        lambda x: parse_chilean_decimal(re.sub(r"m[²2].*", "", x).strip())
    )

    estac = _parse_int(raw.get("estacionamientos") or raw.get("estacionamiento"))

    is_sup_range = sup_lo is not None and sup_hi is not None and sup_lo != sup_hi
    is_m2c_range = m2c_lo is not None and m2c_hi is not None and m2c_lo != m2c_hi

    def src(field): return raw.get(f"{field}_source", raw.get("fetch_source", "detail_html"))

    publisher_candidates = [
        {"source": s, "value": v}
        for s, v in [("info_anunciante", seller_name), ("seller_type", seller_type), ("seller_type_evidence", raw.get("seller_type_evidence", ""))]
        if v
    ]

    now = _utcnow()
    assert origen == "toctoc", f"origen debe ser toctoc, es {origen}"
    source_signal_snapshot = {
        "seller_type": raw.get("seller_type", ""),
        "seller_is_pro": bool(raw.get("seller_is_pro")),
        "company_name": raw.get("company_name", ""),
        "broker_brand": raw.get("broker_brand", ""),
        "publisher_activity": raw.get("publisher_activity", {}),
        "classifier_original_signals": raw.get("classifier_original_signals", {}),
    }
    normalized_classification = normalize_classification(raw.get("classification", {}))
    structured_label = raw.get("comuna_structured_label") or ""
    structured_source = raw.get("comuna_evidence_source") or ""
    structured_commune = bool(
        raw.get("comuna_structured")
        or raw.get("comuna_id") not in (None, "")
        or structured_source == "toctoc_bff"
        or raw.get("comuna_source") == "discovery"
    )
    comuna_slug = normalize_toctoc_commune(
        comuna,
        commune_id=raw.get("comuna_id"),
        structured_label=structured_label,
        structured=structured_commune,
    ) or ""

    return {
        "schema_version": "crm_v1", "run_id": str(raw.get("batch_id") or raw.get("run_id") or now),
        "source": "owner_hunt", "origen": origen, "source_portal": origen,
        "listing_id": listing_id, "listing_id_source": str(raw.get("listing_id_source") or ""),
        "url": url, "canonical_url": url, "title": title,
        "operacion": operacion, "tipo_propiedad": tipo_prop, "comuna": comuna, "region": region,
        "comuna_slug": comuna_slug,
        "fecha_publicacion_raw": "", "fecha_publicacion": "",
        "price": price_display, "precio_raw": price_raw or price_display,
        "precio_moneda_original": precio_moneda_original, "precio_original_num": precio_original_num,
        "precio_uf": price_uf_val, "precio_clp": price_clp_val,
        "precio_uf_raw": price_uf_raw, "precio_clp_raw": price_clp_raw,
        "uf_valor_usado": uf_valor_clp, "uf_fecha": uf_fecha,
        "precio_conversion_source": (
            "toctoc_explicit_both_currencies" if had_original_uf and had_original_clp
            else "calculated_from_uf" if had_original_uf
            else "calculated_from_clp" if had_original_clp
            else "missing"
        ),
        "precio_validacion": price_validation,
        "dormitorios": None if is_dorm_range else dorm_lo,
        "banos": None if is_ban_range else ban_lo,
        "estacionamientos": estac,
        "m2_construidos": None if is_m2c_range else m2c_lo,
        "m2_totales": None if is_sup_range else sup_lo,
        "gastos_comunes": None, "direccion_exacta": comuna,
        "seller_name": seller_name, "publicador_visible": seller_name,
        "seller_text": seller_text, "seller_avatar_alt": seller_avatar,
        "seller_profile_id": str(raw.get("seller_profile_id") or ""),
        "company_name": str(raw.get("company_name") or ""),
        "broker_brand": str(raw.get("broker_brand") or ""),
        "seller_is_pro": bool(raw.get("seller_is_pro")),
        "seller_type": seller_type,
        "seller_type_source": str(raw.get("seller_type_source") or ""),
        "seller_type_evidence": str(raw.get("seller_type_evidence") or ""),
        "description": description, "description_available": len(description) > 50,
        "main_image_url": str(images[0]) if images else "",
        "image_urls": images, "image_urls_count": len(images),
        "classification": normalized_classification,
        "publisher_identity_candidates": publisher_candidates,
        "publisher_activity": source_signal_snapshot["publisher_activity"],
        "source_signals": source_signal_snapshot,
        "html_path": str(raw.get("html_path") or ""),
        "html_sha256": str(raw.get("html_sha256") or ""),
        "description_length": int(raw.get("description_length") or 0),
        "description_is_truncated": bool(raw.get("description_is_truncated")),
        "html_validation_status": str(raw.get("html_validation_status") or ""),
        "html_validation_reason": str(raw.get("html_validation_reason") or ""),
        "fetch_source": str(raw.get("fetch_source") or ""),
        "scrape_stage": "classification_done",
        "processed_at": str(raw.get("processed_at") or now), "updated_at": now,
        "raw_attributes": {}, "manual_review": {"status": "", "notes": "", "reviewed_at": None},
        "source_metadata": {
            "portal": "toctoc", "url_format": str(raw.get("url_format") or ""),
            "listing_id_source": str(raw.get("listing_id_source") or "discovery"),
            "parse_source": str(raw.get("fetch_source") or ""),
            "discovery_page": raw.get("page_number") or 0,
            "publisher_extraction_status": str(raw.get("publisher_extraction_status") or ""),
            "dormitorios_min": dorm_lo if is_dorm_range else None,
            "dormitorios_max": dorm_hi if is_dorm_range else None,
            "dormitorios_raw": dorm_raw if is_dorm_range else "",
            "banos_min": ban_lo if is_ban_range else None,
            "banos_max": ban_hi if is_ban_range else None,
            "banos_raw": ban_raw if is_ban_range else "",
            "superficie_total_min": sup_lo if is_sup_range else None,
            "superficie_total_max": sup_hi if is_sup_range else None,
            "superficie_total_raw": sup_raw if is_sup_range else "",
            "superficie_util_min": m2c_lo if is_m2c_range else None,
            "superficie_util_max": m2c_hi if is_m2c_range else None,
            "superficie_util_raw": m2c_raw if is_m2c_range else "",
            "seller_type_source": str(raw.get("seller_type_source") or ""),
            "seller_type_evidence": str(raw.get("seller_type_evidence") or ""),
            "comuna_source": str(raw.get("comuna_source") or ""),
            "region_source": str(raw.get("region_source") or ""),
            "location_validation": raw.get("location_validation", {}),
            "precio_uf_source": src("precio_uf"), "precio_clp_source": src("precio_clp"),
            "dormitorios_source": src("dormitorios"), "banos_source": src("banos"),
            "surface_source": src("superficie_total"), "title_source": src("title"),
        },
    }
