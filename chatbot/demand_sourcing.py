"""Bounded, read-only demand-to-captation candidate screening.

A portal classification is NOT proof of ownership. Existing executive decisions
take precedence. No portal requests, WhatsApp sends, seller contacts or reassignment.
Only verified, commercially authorized properties may reach a buyer.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

TASK_TYPE = "DEMAND_SOURCING_ADMIN_REVIEW"
_TERMINAL = {
    "corredor", "descartado", "captado", "publicacion expirada",
    "propiedad no disponible", "telefono invalido",
}
_EXISTING = {
    "en gestion", "por contactar", "sin respuesta",
    "contacto exitoso", "reunion agendada",
}
_DNC = re.compile(
    r"corredor(?:a|as|es)?\s+absten(?:erse|ganse)|"
    r"no\s+(?:llamar|contactar|escribir)|"
    r"no\s+(?:quiere|desea|acepta|le interesa)\s+"
    r"(?:trabajar\s+con\s+)?corredor(?:a|as|es)?|"
    r"no\s+le\s+interesa\s+trabajar\s+con\s+corredor(?:a|as|es)?",
)
_TERMS = re.compile(
    r"no\s+(?:quiere|puede|va\s+a)\s+pagar\s+"
    r"(?:el\s+)?(?:\d+(?:[,.]\d+)?\s*%|corretaje|comision)|"
    r"sin\s+comision(?:es)?|no\s+acepta\s+comision(?:es)?",
)
_KIND = {"CASA": "casa", "DEPARTAMENTO": "departamento"}


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _note_text(doc: dict) -> str:
    # Notes are internal-only inputs. Never persist them in review task payloads.
    notes = ((doc.get("gestion") or {}).get("notas") or [])
    return "\n".join(
        str(item.get("content") or "") for item in notes
        if isinstance(item, dict)
    )


def _commune_regex(names: list[str]) -> str:
    # Mongo regex is not accent-insensitive. Make accented vowels optional variants.
    folded = sorted(set(_fold(name) for name in names if name))
    def accent_pattern(value: str) -> str:
        return "".join(
            {"a": "[aá]", "e": "[eé]", "i": "[ií]",
             "o": "[oó]", "u": "[uú]"}.get(c, re.escape(c))
            for c in value
        )
    return "^(?:" + "|".join(accent_pattern(x) for x in folded) + ")$"


def _status(doc: dict) -> str:
    management = doc.get("gestion") or {}
    return _fold(management.get("estado_captacion") or
                 management.get("estado") or "SIN_GESTION")


def _class(doc: dict) -> str:
    classification = doc.get("classification") or {}
    return str(classification.get("effective_state") or
               classification.get("final_state") or
               classification.get("final") or
               classification.get("state") or "INCIERTO").upper()


def _signature(doc: dict) -> str:
    address = _fold(doc.get("direccion_exacta"))
    if len(address) >= 9 and re.search(r"\d", address):
        return "street:" + re.sub(r"[^a-z0-9]", "", address) + ":" + _fold(doc.get("comuna"))
    # Some portals omit direccion_exacta while URLs preserve street+number.
    # Only use a slug as an identity when it contains a street number.
    url = str(doc.get("canonical_url") or doc.get("url") or "")
    parts = urlsplit(url).path.strip("/").split("/")
    last = parts[-1] if parts else ""
    street = parts[-2] if len(parts) > 1 and last.isdigit() else last
    street = re.sub(r"^(?:casas?|departamentos?|deptos?)-", "", _fold(street))
    if len(street) >= 8 and re.search(r"\d", street):
        return "street:" + re.sub(r"[^a-z0-9]", "", street) + ":" + _fold(doc.get("comuna"))
    return "listing:" + str(doc.get("source_portal") or "") + ":" + str(doc.get("listing_id") or "")


def _policy(doc: dict) -> str:
    klass = _class(doc)
    status = _status(doc)
    classification = doc.get("classification") or {}
    if (klass in {"CORREDOR_SEGURO", "CORREDOR_PROBABLE", "BROKER_CONFIRMED"}
            or classification.get("hard_broker_veto") is True
            or classification.get("professional_hard_veto") is True
            or status in _TERMINAL):
        return "EXCLUDED_BROKER_OR_TERMINAL"
    if (doc.get("precio_validacion") or "").upper().startswith("INVALID"):
        return "EXCLUDED_INVALID_PRICE"
    description = _fold(" ".join(str(doc.get(field) or "") for field in
                                 ("description", "descripcion", "body_text_excerpt")))
    notes = _fold(_note_text(doc))
    if _DNC.search(description) or _DNC.search(notes):
        return "EXCLUDED_DO_NOT_CONTACT"
    if status == "no interesado" or _TERMS.search(description) or _TERMS.search(notes):
        return "ADMIN_REVIEW_TERMS"
    if status in _EXISTING:
        return "EXISTING_MANAGEMENT"
    return "VERIFY_OWNER"


def find_demand_captation_candidates(
    db, search: dict, *, limit: int = 12, max_scan: int = 500,
) -> dict:
    """Screen historical external listings, with no changes to the database.

    Priority is buyer compatibility + ownership confidence + management
    eligibility, not mere portal match. Prices and availability remain unverified.
    """
    criteria = search.get("criteria") or {}
    types = [kind for kind in criteria.get("property_types") or [] if kind in _KIND]
    communes = criteria.get("comunas") or []
    maximum = criteria.get("max_price_clp")
    if (search.get("status") != "ACTIVE"
            or criteria.get("operation") != "VENTA"
            or not communes or not types
            or not isinstance(maximum, int)
            or not (10_000_000 <= maximum <= 2_000_000_000)):
        return {"candidates": [], "excluded": {}, "scanned": 0}
    query = {
        "operacion": "venta",
        "tipo_propiedad": {"$in": [_KIND[t] for t in types]},
        "comuna": {"$regex": _commune_regex(communes), "$options": "i"},
        "precio_clp_normalizado": {"$gte": 10_000_000, "$lte": maximum},
        "classification.final_state": {"$nin": ["CORREDOR_SEGURO", "CORREDOR_PROBABLE"]},
        "classification.final": {"$nin": ["CORREDOR_SEGURO", "CORREDOR_PROBABLE"]},
        "classification.effective_state": {"$nin": ["CORREDOR_SEGURO", "CORREDOR_PROBABLE"]},
        "classification.hard_broker_veto": {"$ne": True},
        "classification.professional_hard_veto": {"$ne": True},
        "classification.state": {"$nin": ["CORREDOR_SEGURO", "CORREDOR_PROBABLE"]},
    }
    fields = {
        "_id": 0, "listing_id": 1, "source_portal": 1, "canonical_url": 1,
        "url": 1, "tipo_propiedad": 1, "operacion": 1, "comuna": 1,
        "direccion_exacta": 1, "precio_clp_normalizado": 1,
        "precio_validacion": 1, "classification": 1, "gestion": 1,
        "description": 1, "descripcion": 1, "body_text_excerpt": 1,
        "updated_at": 1,
    }
    results, excluded, signatures, veto_signatures = [], {}, set(), set()
    cursor = db["propiedades_captacion"].find(query, fields).limit(
        max(1, min(max_scan, 1000))
    )
    count = 0
    for doc in cursor:
        count += 1
        policy = _policy(doc)
        signature = _signature(doc)
        if policy.startswith("EXCLUDED"):
            excluded[policy] = excluded.get(policy, 0) + 1
            # A broker / do-not-contact decision for the same street+number
            # also blocks an otherwise uncertain cross-portal duplicate.
            if signature.startswith("street:"):
                veto_signatures.add(signature)
            continue
        if signature in signatures:
            excluded["DUPLICATE"] = excluded.get("DUPLICATE", 0) + 1
            continue
        signatures.add(signature)
        status = _status(doc)
        mgmt = doc.get("gestion") or {}
        prev_owner = str(mgmt.get("ejecutivo_id") or "")
        kind = str(doc.get("tipo_propiedad") or "").lower()
        preferred = criteria.get("preferred_property_type")
        is_preferred = bool(preferred and _KIND.get(preferred) == kind)
        score = (50 if is_preferred else 25)
        if _class(doc) in {"DUEÑO_SEGURO", "DUENO_SEGURO", "DUEÑO_PROBABLE", "DUENO_PROBABLE"}:
            score += 10
        if policy == "VERIFY_OWNER":
            score += 10
        if policy == "EXISTING_MANAGEMENT":
            score -= 15
        if policy == "ADMIN_REVIEW_TERMS":
            score -= 20
        results.append({
            "listing_id": str(doc.get("listing_id") or ""),
            "portal": str(doc.get("source_portal") or ""),
            "url": doc.get("canonical_url") or doc.get("url") or "",
            "type": kind, "comuna": str(doc.get("comuna") or ""),
            "price_clp_reference": doc.get("precio_clp_normalizado"),
            "classification": _class(doc),
            "management_status": status,
            "previous_executive_id": prev_owner,
            "decision": policy,
            "priority_score": score,
            "verification": "RECHECK_PRICE_OWNER_AND_AVAILABILITY",
            "_signature": signature,
        })
    if veto_signatures:
        original = len(results)
        results = [x for x in results if x["_signature"] not in veto_signatures]
        excluded["CROSS_PORTAL_VETO"] = original - len(results)
    for row in results:
        row.pop("_signature", None)
    results.sort(key=lambda p: (-p["priority_score"], p["price_clp_reference"],
                                p["portal"], p["listing_id"]))
    return {"candidates": results[:max(1, min(limit, 30))],
            "excluded": excluded, "scanned": count}


def ensure_admin_demand_review(
    db, *, lead_id, cycle_id: str, owner_id: str,
    search: dict, now: datetime | None = None,
) -> str | None:
    """Idempotent database review queue only; does NOT notify/send or assign.

    Admin will need to approve candidate contact and route new unassigned
    leads to the buyer's executive. Existing management is never overwritten.
    """
    if (search.get("status") != "ACTIVE"
            or not search.get("followup_required")
            or search.get("opt_out") or search.get("matches")):
        return None
    lead = db["leads"].find_one({
        "_id": lead_id,
        "lifecycle.current_assignment_cycle_id": cycle_id,
        "lifecycle.assigned_to_user_id": owner_id,
        "commercial_intelligence.active_search.status": "ACTIVE",
    }, {"_id": 1, "ejecutivo_asignado": 1})
    if not lead:
        return None
    criteria = search.get("criteria") or {}
    signature = "|".join([
        str(lead_id), cycle_id,
        str(criteria.get("operation") or ""),
        str(criteria.get("max_price_clp") or ""),
        ",".join(sorted(criteria.get("comunas") or [])),
        ",".join(sorted(criteria.get("property_types") or [])),
    ])
    identity = "demand_sourcing:" + sha256(signature.encode()).hexdigest()[:32]
    candidates = find_demand_captation_candidates(db, search, limit=12)
    timestamp = now or datetime.now(timezone.utc)
    record = {
        "_id": identity, "task_id": identity, "type": TASK_TYPE,
        "lead_id": lead_id, "assignment_cycle_id": cycle_id,
        "buyer_executive_id": owner_id,
        "buyer_executive_name": str(lead.get("ejecutivo_asignado") or ""),
        "recipient_role": "admin", "status": "pending",
        "source": "verified_active_search",
        "created_at": timestamp, "execute_at": timestamp,
        "contact_approved": False,
        "external_outbound_enabled": False,
        "note": "Demanda activa sin stock: revisar candidatos históricos y "
                "coordinar captación con el ejecutivo del comprador. "
                "No contactar por sistema ni revelar datos del comprador.",
        "criteria": {
            "operation": criteria.get("operation"),
            "max_price_clp": criteria.get("max_price_clp"),
            "comunas": criteria.get("comunas") or [],
            "property_types": criteria.get("property_types") or [],
            "preferred_property_type": criteria.get("preferred_property_type"),
            "location_targets": criteria.get("location_targets") or [],
        },
        "review_scope": ("LOCATION_OR_BUDGET_NEEDS_HUMAN_VERIFICATION"
                         if criteria.get("location_targets") and
                         (not criteria.get("max_price_clp") or
                          not criteria.get("comunas")) else
                         "HISTORICAL_LISTINGS_REVALIDATION"),
        "candidate_refs": candidates["candidates"],
        "screened_count": candidates["scanned"],
        "excluded_counts": candidates["excluded"],
    }
    db["crm_tasks"].update_one(
        {"_id": identity}, {"$setOnInsert": record}, upsert=True,
    )
    return identity
