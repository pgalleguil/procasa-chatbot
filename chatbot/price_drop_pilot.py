"""Contexto opt-in para el piloto de WhatsApp por rebaja de precio.

Lectura exclusivamente: este módulo no envía mensajes, no crea leads y no
modifica titularidad, ciclos de asignación ni SLA.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

from .phone_utils import normalize_phone_strict, phone_identity_variants

COLLECTION = "crm_price_drop_pilot_v1"
FLAG = "PRICE_DROP_PILOT_ENABLED"
TEST_PHONE = "PRICE_DROP_PILOT_TEST_PHONE"
_VERSION = "price_drop_pilot_v1"


def _enabled_for_phone(phone: str) -> bool:
    if os.environ.get(FLAG, "").lower() not in {"1", "true", "yes"}:
        return False
    tester = normalize_phone_strict(os.environ.get(TEST_PHONE, ""))
    recipient = normalize_phone_strict(phone)
    # No test phone configured -> fail closed. Not a general campaign toggle.
    return bool(tester and recipient and tester == recipient)


def _money(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def validate_record(record: dict, property_doc: dict, lead_id: Any) -> bool:
    """Check provenance and the live published sale price, never recommendations."""
    if not record or record.get("status") != "SENT":
        return False
    if not record.get("provider_message_id"):
        return False
    if str(record.get("lead_id") or "") != str(lead_id or ""):
        return False
    if str(record.get("operation") or "").lower() != "venta":
        return False
    old = _money(record.get("old_price_uf"))
    new = _money(record.get("new_price_uf"))
    if old is None or new is None or not (old > new and 2000 <= new <= 5000):
        return False
    if not property_doc or str(property_doc.get("codigo")) != str(record.get("property_code")):
        return False
    estado = property_doc.get("estado") or {}
    tipo = property_doc.get("tipo_operacion") or {}
    if not property_doc.get("disponible_prop360"):
        return False
    if estado.get("oficina") != "PROCASA SUCRE" or tipo.get("venta") is not True:
        return False
    live = _money((tipo.get("precio_venta") or {}).get("precio_uf"))
    return live is not None and abs(live - new) <= 0.01


def _other_property_referenced(message: str, campaign_code: str) -> bool:
    """A customer's new explicit link/code beats the earlier campaign."""
    if re.search(r"https?://|www\.", message or "", re.I):
        return True
    m = re.search(r"\b(?:c[oó]digo|cod\.?|propiedad\s+(?:n[uú]mero|nro\.?))\s*[:#]?\s*(\d{4,6})\b", message or "", re.I)
    return bool(m and m.group(1) != campaign_code)


def load_active_context(db: Any, phone: str, message: str, *, now: datetime | None = None) -> dict | None:
    """Only one identified test lead, one delivered message and an active sale."""
    if not _enabled_for_phone(phone):
        return None
    now = now or datetime.now(timezone.utc)
    variants = phone_identity_variants(phone)
    # Historical duplicates may use +569... or 569...; refuse all ambiguous identities.
    leads = list(db["leads"].find({
        "$or": [
            {"phone": {"$in": variants}},
            {"contact_phone_normalized": {"$in": variants}},
        ]
    }, {"_id": 1}).limit(2))
    if len(leads) != 1:
        return None
    lead_id = leads[0]["_id"]
    record = db[COLLECTION].find_one(
        {
            "phone_normalized": normalize_phone_strict(phone),
            "lead_id": lead_id,
            "status": "SENT",
            "sent_at": {"$lte": now},
            "expires_at": {"$gt": now},
        },
        sort=[("sent_at", -1)],
    )
    if not record or _other_property_referenced(message, str(record.get("property_code") or "")):
        return None
    from .property_lookup import PROPERTY_COLLECTION_NAME
    property_doc = db[PROPERTY_COLLECTION_NAME].find_one(
        {"codigo": str(record.get("property_code") or "")},
        {"codigo": 1, "estado.oficina": 1, "disponible_prop360": 1,
         "tipo_operacion.venta": 1, "tipo_operacion.precio_venta.precio_uf": 1},
    )
    if not validate_record(record, property_doc, lead_id):
        return None
    return {
        "version": _VERSION,
        "campaign_id": str(record.get("_id")),
        "property_code": str(record["property_code"]),
        "old_price_uf": float(record["old_price_uf"]),
        "new_price_uf": float(record["new_price_uf"]),
        "operation": "Venta",
    }


def _format_uf(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".").removesuffix(",00")


def context_instruction(context: dict) -> str:
    code = context["property_code"]
    old_price = _format_uf(context["old_price_uf"])
    new_price = _format_uf(context["new_price_uf"])
    return (
        "[PILOTO DE REACTIVACIÓN POR REBAJA - CONTEXTO VERIFICADO]\n"
        f"El cliente recibió un WhatsApp informativo sobre la propiedad en VENTA código {code}, "
        f"rebajada de UF {old_price} a UF {new_price}. El precio actual fue cotejado con Prop360. "
        "Su mensaje actual puede ser respuesta a esa comunicación aunque sea 'sí', 'me interesa' o 'cuéntame'. "
        "No lo trates como un lead nuevo ni solicites de nuevo su teléfono. "
        "Un 'sí' ambiguo expresa interés informativo, NO confirma una visita, oferta ni reserva. "
        "Responde sobre esta propiedad y ofrece información o coordinar visita. "
        "Si pide visita, consulta únicamente día o rango horario y deriva según el flujo CRM existente; "
        "no prometas una visita confirmada. Si rechaza o pide no recibir mensajes, respétalo. "
        "Si pregunta por otras propiedades, prioriza su petición nueva. "
        "No afirmes que se envió el WhatsApp si el cliente lo niega; ofrece verificarlo."
    )
