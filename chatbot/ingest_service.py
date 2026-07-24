"""
Servicio canónico de ingesta de leads.

Punto único de entrada para todos los canales:
- Prop360 (extractor HTTP)
- WhatsApp (webhook)
- Ingreso manual
- Portales futuros

Uso:
    result = ingest_lead_event(LeadEvent(
        source_system="prop360",
        source_event_id="7151",
        phone="+56912345678",
        email="cliente@mail.com",
        name="Juan Pérez",
        message="Interesado en propiedad",
        property_code="6600",
        portal_source="Portal Inmobiliario",
        contact_date="2026-07-23T22:12:18",
    ))
"""

import hashlib
import logging
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field, asdict

from .constants import CHILE_TZ, PipelineStage, UNASSIGNED_LABEL
from .storage import get_db, COLLECTION_CONVERSATIONS, log_event
from .phone_utils import normalize_phone_strict, is_synthetic_phone, build_synthetic_phone_key
from .lead_temperature import effective_temperature_set
from .lead_router import find_responsible_executive, get_executive_phone, get_next_business_slot
from .property_lookup import (
    PROPERTY_COLLECTION_NAME,
    find_property_by_any_identifier,
    find_property_in_any_collection,
    get_prop_location,
    get_prop_operation,
    get_prop_executive,
)

IDEMPOTENCY_COLLECTION = "lead_ingest_events"

logger = logging.getLogger(__name__)

INVALID_PROPERTY_CODES = {"", "N/D", "NONE", "NULL", "S/N", "ND", "-", "0"}

HOT_INTENT_KEYWORDS = [
    "visitar", "agendar", "ver la propiedad", "ver departamento", "ver casa",
    "quiero comprar", "quiero arrendar", "oferta", "cotizar", "contactar",
    "llamar", "mi número", "comunicarme", "ubicación", "dirección",
    "disponible", "cuánto cuesta", "precio", "valor",
]


def _has_hot_intent(message: str) -> bool:
    if not message:
        return False
    msg_lower = message.lower()
    for keyword in HOT_INTENT_KEYWORDS:
        if keyword in msg_lower:
            return True
    return False


def ensure_idempotency_index():
    """Crea índice único para idempotencia de eventos fuente."""
    db = get_db()
    collection = db[COLLECTION_CONVERSATIONS]
    existing_indexes = collection.index_information()
    if "uq_source_events" not in existing_indexes:
        collection.create_index(
            [("source_events.source_system", 1), ("source_events.source_event_id", 1)],
            name="uq_source_events",
            unique=True,
            partialFilterExpression={
                "source_events.source_system": {"$exists": True},
                "source_events.source_event_id": {"$exists": True},
            },
        )
        logger.info("[INGEST] Índice único uq_source_events creado")
    if "idx_phone" not in existing_indexes:
        collection.create_index("phone", name="idx_phone")
        logger.info("[INGEST] Índice idx_phone creado")
    if "idx_prospecto_email" not in existing_indexes:
        collection.create_index("prospecto.email", name="idx_prospecto_email")
        logger.info("[INGEST] Índice idx_prospecto_email creado")

    ledger = db[IDEMPOTENCY_COLLECTION]
    ledger_indexes = ledger.index_information()
    if "uq_ingest_ledger" not in ledger_indexes:
        ledger.create_index(
            [("source_system", 1), ("source_event_id", 1)],
            name="uq_ingest_ledger",
            unique=True,
        )
        logger.info("[INGEST] Índice único uq_ingest_ledger creado en %s", IDEMPOTENCY_COLLECTION)


@dataclass
class LeadEvent:
    source_system: str
    source_event_id: str
    phone: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    message: Optional[str] = None
    property_code: Optional[str] = None
    portal_source: Optional[str] = None
    contact_date: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestResult:
    status: str  # created | updated | duplicate_event | conflict | rejected | error
    lead_id: Optional[str] = None
    is_new_lead: bool = False
    identity_match: str = "none"  # phone | email | both | source_event | none
    property_found: bool = False
    assignment_changed: bool = False
    temperature: str = "COLD"  # HOT | COLD
    executive: Optional[str] = None
    identity_conflict: bool = False
    conflict_details: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


def _normalize_email(raw: Optional[str]) -> Tuple[Optional[str], bool]:
    if not raw:
        return None, False
    email = str(raw).strip().lower()
    if "@" not in email or "." not in email:
        return None, False
    return email, True


def _is_valid_property_code(code: Optional[str]) -> bool:
    if not code:
        return False
    return str(code).strip().upper() not in INVALID_PROPERTY_CODES


def _find_lead_by_id(phone: Optional[str], email: Optional[str]) -> Tuple[Optional[Dict], Optional[Dict], Optional[str]]:
    """
    Busca en leads por teléfono y/o email.
    Retorna (lead_por_phone, lead_por_email, conflict_status).
    """
    db = get_db()
    lead_by_phone = None
    lead_by_email = None

    if phone:
        lead_by_phone = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone})

    if email:
        lead_by_email = db[COLLECTION_CONVERSATIONS].find_one({"prospecto.email": email})

    return lead_by_phone, lead_by_email, None


def _find_by_source_event(source_system: str, source_event_id: str) -> Optional[Dict]:
    """Busca un lead por idempotencia del evento fuente."""
    db = get_db()
    return db[COLLECTION_CONVERSATIONS].find_one({
        "source_events": {
            "$elemMatch": {
                "source_system": source_system,
                "source_event_id": source_event_id,
            }
        }
    })


def _atomic_reserve_event(source_system: str, source_event_id: str) -> bool:
    """Intenta reservar un evento en el ledger técnico. Operación atómica.
    Retorna True si se reservó (primera vez), False si ya existía."""
    db = get_db()
    try:
        db[IDEMPOTENCY_COLLECTION].insert_one({
            "source_system": source_system,
            "source_event_id": str(source_event_id),
            "reserved_at": datetime.now(CHILE_TZ).isoformat(),
            "status": "processing",
        })
        return True
    except Exception:
        return False


def _finalize_event(source_system: str, source_event_id: str, lead_id: str, status: str):
    """Actualiza el ledger técnico con el resultado del procesamiento."""
    db = get_db()
    db[IDEMPOTENCY_COLLECTION].update_one(
        {"source_system": source_system, "source_event_id": str(source_event_id)},
        {"$set": {
            "status": status,
            "lead_id": lead_id,
            "completed_at": datetime.now(CHILE_TZ).isoformat(),
        }},
    )


def _enrich_from_cartera(db, property_code: str) -> Dict[str, Any]:
    """Busca una propiedad en las colecciones de cartera y retorna datos enriquecidos."""
    if not _is_valid_property_code(property_code):
        return {"property_found": False}

    prop = find_property_in_any_collection(db, property_code)
    if not prop:
        return {"property_found": False}

    location = get_prop_location(prop)
    operation = get_prop_operation(prop)
    executive = get_prop_executive(prop)

    return {
        "property_found": True,
        "comuna": location.get("comuna", ""),
        "region": location.get("region", ""),
        "direccion": location.get("direccion", ""),
        "tipo": operation.get("tipo", ""),
        "operacion": operation.get("operacion", ""),
        "precio_uf": operation.get("precio_uf"),
        "precio_clp": operation.get("precio_clp"),
        "ejecutivo_ficha": executive,
        "canonical_code": str(prop.get("codigo", property_code)),
    }


def ingest_lead_event(event: LeadEvent) -> IngestResult:
    """
    Punto único de entrada para todos los canales.
    Normaliza, busca duplicados, cruza propiedad, asigna, crea/actualiza.
    """
    db = get_db()
    now = datetime.now(CHILE_TZ)
    now_iso = now.isoformat()

    phone_raw = str(event.phone or "").strip()
    phone_normalized = normalize_phone_strict(phone_raw)
    email, email_valid = _normalize_email(event.email)
    name = (event.name or "").strip()
    message = (event.message or "").strip()
    property_code = str(event.property_code or "").strip()
    portal_source = (event.portal_source or event.metadata.get("medio") or "").strip()
    contact_date = event.contact_date or now_iso

    if not _atomic_reserve_event(event.source_system, event.source_event_id):
        existing_by_source = _find_by_source_event(event.source_system, event.source_event_id)
        if existing_by_source:
            return IngestResult(
                status="duplicate_event",
                lead_id=str(existing_by_source["_id"]),
                identity_match="source_event",
            )
        return IngestResult(
            status="rejected",
            error="Evento ya reservado pero lead no encontrado",
        )

    phone_has_real = bool(phone_normalized) and not is_synthetic_phone(phone_normalized)
    phone_for_key = phone_normalized if phone_has_real else None
    if not phone_for_key and phone_raw and not phone_normalized:
        phone_for_key = phone_raw

    if phone_has_real:
        lead_by_phone, lead_by_email, _ = _find_lead_by_id(phone_normalized, email)
    else:
        lead_by_phone = None
        _, lead_by_email, _ = _find_lead_by_id(None, email)
    identity_conflict = False
    conflict_details = None
    target_lead = None
    action = None

    if phone_normalized and email and lead_by_phone and lead_by_email:
        lead_phone_id = str(lead_by_phone["_id"])
        lead_email_id = str(lead_by_email["_id"])
        if lead_phone_id != lead_email_id:
            identity_conflict = True
            conflict_details = {
                "phone_lead_id": lead_phone_id,
                "email_lead_id": lead_email_id,
                "phone": phone_normalized or phone_raw,
                "email": email,
            }
            for candidate_id in [lead_phone_id, lead_email_id]:
                db[COLLECTION_CONVERSATIONS].update_one(
                    {"_id": lead_by_phone["_id"] if candidate_id == lead_phone_id else lead_by_email["_id"]},
                    {"$addToSet": {
                        "identity_conflicts": {
                            "source_system": event.source_system,
                            "source_event_id": event.source_event_id,
                            "detected_at": now_iso,
                            "other_lead_id": lead_email_id if candidate_id == lead_phone_id else lead_phone_id,
                            "reason": "phone_email_mismatch",
                        }
                    }}
                )
            return IngestResult(
                status="conflict",
                identity_match="both",
                identity_conflict=True,
                conflict_details=conflict_details,
                temperature="COLD",
            )

    if lead_by_phone:
        target_lead = lead_by_phone
        action = "updated_phone_match"
    elif lead_by_email:
        target_lead = lead_by_email
        action = "updated_email_match"
    else:
        action = "created"

    property_enrich = _enrich_from_cartera(db, property_code)

    if target_lead:
        lead_id = str(target_lead["_id"])
        update_fields: Dict[str, Any] = {
            "last_message_at": now_iso,
            "last_crm_update": now,
            "updated_at": now_iso,
        }

        if email:
            update_fields["prospecto.email"] = email
        if name and len(name) >= 2:
            update_fields["prospecto.nombre"] = name.title()

        if property_code and _is_valid_property_code(property_code):
            update_fields["prospecto.codigo"] = property_code
            update_fields["prospecto.comuna"] = property_enrich.get("comuna", target_lead.get("prospecto", {}).get("comuna", ""))
            update_fields["prospecto.region"] = property_enrich.get("region", "")
            update_fields["prospecto.tipo"] = property_enrich.get("tipo", "")
            update_fields["prospecto.operacion"] = property_enrich.get("operacion", "")
            if phone_normalized:
                db[COLLECTION_CONVERSATIONS].update_one(
                    {"_id": target_lead["_id"]},
                    {"$addToSet": {"prospecto.propiedades_vistas": property_code}},
                )

        if portal_source:
            update_fields["origen"] = portal_source

        update_fields["$addToSet"] = {
            "source_events": {
                "source_system": event.source_system,
                "source_event_id": event.source_event_id,
                "contact_date": contact_date,
                "portal_source": portal_source,
                "message_preview": message[:160],
                "ingested_at": now_iso,
            }
        }

        message_entry = {
            "role": "user",
            "content": message or f"Contacto desde {portal_source or event.source_system}",
            "timestamp": contact_date,
            "source": event.source_system,
            "source_event_id": event.source_event_id,
            "portal": portal_source,
        }

        current_exec = target_lead.get("ejecutivo_asignado") or target_lead.get("prospecto", {}).get("ejecutivo")
        unassigned = {UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", None, ""}

        if current_exec in unassigned or property_code != target_lead.get("prospecto", {}).get("codigo"):
            if _is_valid_property_code(property_code) and property_enrich["property_found"]:
                exec_name, exec_phone, assignment_type = find_responsible_executive(
                    property_code=property_code,
                    comuna=property_enrich.get("comuna", ""),
                    lead_phone=phone_normalized or phone_raw,
                    lead_name=name,
                )
                if exec_name not in unassigned:
                    update_fields["ejecutivo_asignado"] = exec_name
                    update_fields["prospecto.ejecutivo"] = exec_name
                    assigned_at = get_next_business_slot(now)
                    update_fields["lifecycle.assigned_at"] = assigned_at.isoformat()
                    if not current_exec or current_exec in unassigned:
                        update_fields["lifecycle.created_at"] = now_iso
        else:
            exec_name = current_exec

        hot_detected = _has_hot_intent(message)
        initial_temp = "HOT" if hot_detected else "COLD"
        temp_set = effective_temperature_set(initial_temp)
        update_fields.update(temp_set)
        if hot_detected:
            update_fields["last_intent"] = "ASK_VISIT"

        update_payload = {"$set": {}, "$push": {"messages": {"$each": [message_entry], "$slice": -50}}}
        for k, v in update_fields.items():
            if k != "$addToSet":
                update_payload["$set"][k] = v
        if "$addToSet" in update_fields:
            update_payload["$addToSet"] = update_fields["$addToSet"]

        db[COLLECTION_CONVERSATIONS].update_one({"_id": target_lead["_id"]}, update_payload)

        log_event(
            phone_normalized or email or "unknown",
            "LEAD_INGEST",
            event.source_system,
            {"action": action, "source_event_id": event.source_event_id, "property_code": property_code},
            lead_id=target_lead["_id"],
        )

        identity = "phone" if phone_normalized else ("email" if email else "none")
        if phone_normalized and email:
            identity = "both" if lead_by_phone and lead_by_email and str(lead_by_phone["_id"]) == str(lead_by_email["_id"]) else identity
        _finalize_event(event.source_system, event.source_event_id, lead_id, "updated")
        return IngestResult(
            status="updated",
            lead_id=lead_id,
            identity_match=identity,
            executive=update_fields.get("ejecutivo_asignado", current_exec),
            property_found=property_enrich["property_found"],
            temperature=initial_temp,
            assignment_changed=(update_fields.get("ejecutivo_asignado") != target_lead.get("ejecutivo_asignado")),
        )

    exec_name, exec_phone, assignment_type = UNASSIGNED_LABEL, None, "NO_PROPERTY"
    if _is_valid_property_code(property_code) and property_enrich["property_found"]:
        exec_name, exec_phone, assignment_type = find_responsible_executive(
            property_code=property_code,
            comuna=property_enrich.get("comuna", ""),
            lead_phone=phone_normalized or phone_raw,
            lead_name=name,
        )
    elif _is_valid_property_code(property_code) and not property_enrich["property_found"]:
        exec_name = UNASSIGNED_LABEL
        assignment_type = "MISSING_PROPERTY"

    assigned_at = get_next_business_slot(now)
    if phone_normalized:
        final_phone = phone_normalized
        phone_is_synthetic = False
        contact_identity_incomplete = False
    else:
        final_phone = build_synthetic_phone_key(event.source_system, event.source_event_id)
        phone_is_synthetic = True
        contact_identity_incomplete = True

    hot_detected = _has_hot_intent(message)
    initial_temp = "HOT" if hot_detected else "COLD"
    temp_set = effective_temperature_set(initial_temp)

    lead_doc: Dict[str, Any] = {
        "phone": final_phone,
        "phone_is_synthetic": phone_is_synthetic,
        "contact_phone": phone_raw if phone_raw else None,
        "contact_phone_normalized": phone_normalized,
        "contact_identity_incomplete": contact_identity_incomplete,
        "contact_identity_incomplete": contact_identity_incomplete,
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_crm_update": now,
        "last_message_at": contact_date,
        "source_type": event.source_system,
        "origen": portal_source or event.source_system,
        "ejecutivo_asignado": exec_name,
        "pipeline_stage": PipelineStage.NEW,
        "stage": PipelineStage.NEW,
        **temp_set,
        "prospecto": {
            "nombre": name,
            "email": email or "",
            "phone": phone_raw or "",
            "codigo": property_code,
            "codigo": property_code,
            "ejecutivo": exec_name,
            "comuna": property_enrich.get("comuna", ""),
            "region": property_enrich.get("region", ""),
            "tipo": property_enrich.get("tipo", ""),
            "operacion": property_enrich.get("operacion", ""),
            "canal_origen": portal_source or event.source_system,
        },
        "lifecycle": {
            "created_at": now_iso,
            "assigned_at": assigned_at.isoformat(),
        },
        "messages": [
            {
                "role": "user",
                "content": message or f"Contacto desde {portal_source or event.source_system}",
                "timestamp": contact_date,
                "source": event.source_system,
                "source_event_id": event.source_event_id,
                "portal": portal_source,
            }
        ],
        "stage_history": [
            {
                "from": None,
                "to": PipelineStage.NEW,
                "actor": event.source_system,
                "timestamp": now_iso,
                "notes": f"Ingreso automático desde {event.source_system}",
            }
        ],
        "source_events": [
            {
                "source_system": event.source_system,
                "source_event_id": event.source_event_id,
                "contact_date": contact_date,
                "portal_source": portal_source,
                "message_preview": message[:160],
                "ingested_at": now_iso,
            }
        ],
        "property_found": property_enrich["property_found"],
        "cartera_data": property_enrich if property_enrich["property_found"] else {},
    }

    if hot_detected:
        lead_doc["last_intent"] = "ASK_VISIT"

    if _is_valid_property_code(property_code):
        lead_doc["prospecto"]["propiedades_vistas"] = [property_code]

    try:
        result = db[COLLECTION_CONVERSATIONS].insert_one(lead_doc)
        lead_id = str(result.inserted_id)

        log_event(
            phone_normalized or email or "unknown",
            "LEAD_CREATED",
            event.source_system,
            {"source_event_id": event.source_event_id, "property_code": property_code, "assigned_to": exec_name},
            lead_id=lead_id,
        )

        identity = "phone" if phone_normalized else ("email" if email else "none")
        if phone_normalized and email:
            identity = "both"
        _finalize_event(event.source_system, event.source_event_id, lead_id, "created")
        return IngestResult(
            status="created",
            lead_id=lead_id,
            is_new_lead=True,
            identity_match=identity,
            executive=exec_name,
            property_found=property_enrich["property_found"],
            temperature=initial_temp,
            assignment_changed=True,
        )
    except Exception as e:
        logger.error(f"[INGEST] Error creando lead: {e}", exc_info=True)
        return IngestResult(
            status="error",
            error=str(e),
        )
