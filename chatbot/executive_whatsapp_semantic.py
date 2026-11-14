"""Strict, PII-minimized semantic candidate for later Atlas Vector Search.

This does NOT create embeddings or external API calls. Never vectorize raw
WhatsApp messages, phone numbers, names, freeform notes, or unreviewed LLM text.
A single lead can generate at most one current commercial profile candidate.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Mapping

_ALLOWED_STATUS = {
    "ACTIVE_INTEREST", "VISIT_COORDINATED", "FOLLOW_UP_PENDING",
    "NO_EFFECTIVE_CONTACT", "NO_LONGER_SEARCHING",
    "TRANSACTION_COMPLETED", "UNKNOWN",
}
_ALLOWED_OPERATION = {"venta", "arriendo"}
_ALLOWED_PROPERTY_TYPE = {
    "departamento", "casa", "oficina", "local comercial", "terreno",
    "parcela", "bodega",
}
_ALLOWED_FEATURES = {
    "estacionamiento", "bodega", "terraza", "patio", "jardin",
    "piscina", "metro", "mascotas", "ascensor",
}
# Fixed allow-list prevents accidentally vectorizing names or street addresses.
_ALLOWED_COMMUNES = {
    "maipu", "padre hurtado", "penaflor", "cerrillos", "lo espejo",
    "san bernardo", "santiago", "providencia", "nunoa", "la reina",
    "las condes", "vitacura", "macul", "san miguel", "la florida",
    "puente alto", "penalolen", "lo prado", "estacion central",
    "quinta normal", "pudahuel", "quilicura", "independencia",
    "recoleta", "la cisterna", "el bosque", "la granja", "la pintana",
    "san joaquin", "huechuraba", "renca", "conchali", "la florida",
    "talca", "curico", "rancagua", "vina del mar", "valparaiso",
}

PROFILE_SCHEMA_VERSION = "lead_commercial_semantic_v1"
MAX_VECTOR_CANDIDATE_LENGTH = 600


def _normalized_token(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    text = unicodedata.normalize("NFKD", raw.strip().lower())
    return " ".join(
        "".join(ch for ch in text if not unicodedata.combining(ch)).split()
    )


def _safe_int(value: Any, minimum: int, maximum: int) -> int | None:
    if type(value) is int and minimum <= value <= maximum:
        return value
    return None


def build_semantic_candidate(
    *,
    lead_id: str,
    audit_result: Mapping[str, Any],
    reviewed_preferences: Mapping[str, Any],
    reviewed_at: datetime,
) -> dict[str, Any] | None:
    """Return a bounded, verified profile, or None when processing is unsafe.

    The caller must confirm a human review and has to supply a purpose-built,
    allow-listed preference mapping; never pass arbitrary chat or LLM summary.
    """
    if not isinstance(lead_id, str) or not lead_id.strip():
        raise ValueError("lead_id_required")
    if not isinstance(audit_result, Mapping) or not isinstance(reviewed_preferences, Mapping):
        raise ValueError("structured_review_required")
    if not isinstance(reviewed_at, datetime) or reviewed_at.utcoffset() is None:
        raise ValueError("reviewed_at_requires_timezone")

    if audit_result.get("review_status") != "APPROVED_HUMAN_REVIEW":
        return None
    if audit_result.get("opt_out") is not False:
        return None
    status = audit_result.get("commercial_status")
    if status not in _ALLOWED_STATUS or status in (
        "NO_LONGER_SEARCHING", "TRANSACTION_COMPLETED", "UNKNOWN",
    ):
        return None
    evidence = audit_result.get("evidence_provider_keys")
    if not isinstance(evidence, list) or not evidence or any(
        not isinstance(key, str) or not key.strip() for key in evidence
    ):
        return None

    operation = _normalized_token(reviewed_preferences.get("operacion"))
    kind = _normalized_token(reviewed_preferences.get("tipo_propiedad"))
    raw_communes = reviewed_preferences.get("comunas") or []
    raw_features = reviewed_preferences.get("caracteristicas") or []
    if not isinstance(raw_communes, list) or not isinstance(raw_features, list):
        raise ValueError("structured_lists_required")
    communes = sorted(
        {v for raw in raw_communes if (v := _normalized_token(raw)) in _ALLOWED_COMMUNES}
    )[:6]
    features = sorted(
        {v for raw in raw_features if (v := _normalized_token(raw)) in _ALLOWED_FEATURES}
    )[:8]
    max_uf = _safe_int(reviewed_preferences.get("presupuesto_max_uf"), 100, 100_000)
    bedrooms = _safe_int(reviewed_preferences.get("dormitorios_min"), 0, 15)

    parts: list[str] = ["Busqueda inmobiliaria"]
    if operation in _ALLOWED_OPERATION:
        parts.append("operacion " + operation)
    if kind in _ALLOWED_PROPERTY_TYPE:
        parts.append("tipo " + kind)
    if communes:
        parts.append("comunas " + ", ".join(communes))
    if max_uf is not None:
        parts.append("presupuesto maximo UF " + str(max_uf))
    if bedrooms is not None:
        parts.append("dormitorios minimo " + str(bedrooms))
    if features:
        parts.append("caracteristicas " + ", ".join(features))

    # A bare status/heading without an explicit property preference is not a
    # useful embedding; avoid uploading unnecessarily personal CRM metadata.
    if len(parts) <= 1:
        return None
    vector_text = ". ".join(parts)[:MAX_VECTOR_CANDIDATE_LENGTH]
    digest_input = json.dumps({
        "schema": PROFILE_SCHEMA_VERSION,
        "text": vector_text,
        "evidence": sorted(set(evidence)),
        "reviewed_at": reviewed_at.astimezone(timezone.utc).isoformat(),
    }, sort_keys=True, ensure_ascii=False)
    revision = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "lead_id": lead_id,
        "commercial_status": status,
        "vector_text": vector_text,
        "source_evidence_keys": sorted(set(evidence)),
        "reviewed_at": reviewed_at.astimezone(timezone.utc).isoformat(),
        "revision_sha256": revision,
        "embedding_generated": False,
        "index_write_performed": False,
        "purpose": "audited_property_recommendation_search",
    }
