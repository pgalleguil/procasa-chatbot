"""Zero-CRM side-effect WhatsApp smoke test for the price-drop campaign.

This module is **not** the normal lead handler. Only the explicitly enabled
test phone is intercepted at the webhook boundary, before event_log,
commercial_intake, queue, CRM/SLA, chatbot.core, and notification services.
The separate collections are private to this isolated test.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

from pymongo.errors import DuplicateKeyError

from .phone_utils import normalize_phone_strict
from .price_drop_pilot import (
    COLLECTION, _enabled_for_phone, _format_uf, _money,
    _other_property_referenced,
)

logger = logging.getLogger(__name__)
TURN_COLLECTION = "crm_price_drop_pilot_test_turns_v1"
STOP = re.compile(r"\b(?:stop|no me escriban|dejen de escribir|no quiero (?:más |mas )?mensajes|darme de baja|eliminar mi n[uú]mero)\b", re.I)
DECLINE = re.compile(r"^\s*(?:no|no gracias|no me interesa|ya no me interesa|no estoy interesado|por ahora no)\b", re.I)
VISIT = re.compile(r"\b(?:visitar|visita|agendar|coordinar|quiero verla|ir a ver|conocerla|ver el departamento)\b", re.I)
PRICE = re.compile(r"\b(?:precio|cu[aá]nto (?:cuesta|vale|sale)|a cu[aá]nto|rebaja|baj[oó]|uf)\b", re.I)
NEGOTIATION = re.compile(r"\b(?:oferta|negociar|rebajar|aceptan menos|descuento|contraoferta)\b", re.I)
YES = re.compile(r"^\s*(?:s[ií]|s[ií],?\s*me interesa|claro|ok|dale|por supuesto|me interesa|perfecto)\s*[.!]?\s*$", re.I)
ALTERNATIVE = re.compile(r"\b(?:otra|otras|alternativas|opciones|ñuñoa|nunoa)\b", re.I)
BAD_CLAIM = re.compile(
    r"\b(?:he (?:avisado|notificado|contactado|agendado|registrado|derivado)|"
    r"(?:ya |est[aá] )?(?:agendada|confirmada) la visita|"
    r"el ejecutivo (?:fue notificado|te (?:llamar[aá]|contactar[aá])))\b", re.I
)


def is_test_phone(phone: str) -> bool:
    """Only configured recipient; no lookup or write for unrelated numbers."""
    return _enabled_for_phone(phone)


def load_test_context(db: Any, phone: str, *, now: datetime | None = None) -> dict | None:
    """Resolve *only* a verified test send; never inspect the leads collection."""
    if not is_test_phone(phone):
        return None
    now = now or datetime.now(timezone.utc)
    canon = normalize_phone_strict(phone)
    campaign = db[COLLECTION].find_one({
        "phone_normalized": canon,
        "test_mode": True,
        "lead_id": {"$exists": False},
        "status": "SENT",
        "expires_at": {"$gt": now},
    }, sort=[("sent_at", -1)])
    if not campaign or not campaign.get("provider_message_id"):
        return None
    if normalize_phone_strict(campaign.get("phone_normalized") or "") != canon:
        return None
    sent_at, expires_at = campaign.get("sent_at"), campaign.get("expires_at")
    if not isinstance(sent_at, datetime) or not isinstance(expires_at, datetime):
        return None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not sent_at <= now < expires_at:
        return None
    if campaign.get("operation") != "Venta":
        return None
    old, new = _money(campaign.get("old_price_uf")), _money(campaign.get("new_price_uf"))
    if old is None or new is None or not (old > new and 2000 <= new <= 5000):
        return None
    from .property_lookup import PROPERTY_COLLECTION_NAME
    property_doc = db[PROPERTY_COLLECTION_NAME].find_one(
        {"codigo": str(campaign.get("property_code") or "")},
        {"codigo": 1, "disponible_prop360": 1, "estado.oficina": 1,
         "tipo_operacion.venta": 1, "tipo_operacion.precio_venta.precio_uf": 1},
    ) or {}
    tipo = property_doc.get("tipo_operacion") or {}
    price = _money((tipo.get("precio_venta") or {}).get("precio_uf"))
    if (not property_doc.get("disponible_prop360") or
        (property_doc.get("estado") or {}).get("oficina") != "PROCASA SUCRE" or
        tipo.get("venta") is not True or price is None or abs(price - new) > 0.01):
        return None
    return {
        "campaign_id": campaign["_id"],
        "property_code": str(campaign["property_code"]),
        "old_price_uf": old, "new_price_uf": new,
    }


def _safe_fallback(ctx: dict) -> str:
    return (
        f"Esta es una prueba interna sobre el departamento {ctx['property_code']}, "
        f"actualmente en UF {_format_uf(ctx['new_price_uf'])}. "
        "¿Te gustaría recibir información o coordinar una visita? "
        "En esta prueba no notificaremos a ejecutivos ni gestionaremos una visita real."
    )


def _llm_answer(message: str, ctx: dict, history: list[dict]) -> str:
    """Use the production language-model provider but no production CRM pipeline."""
    from config import Config
    from .grok_client import client
    code = ctx["property_code"]
    price = _format_uf(ctx["new_price_uf"])
    old = _format_uf(ctx["old_price_uf"])
    sys = (
        "Eres el asistente de PROCASA, en una PRUEBA INTERNA por WhatsApp. "
        f"La propiedad es un departamento en VENTA en San Miguel, código {code}. "
        f"Precio anterior UF {old}, precio ACTUAL UF {price}. "
        "Estas son las únicas características confirmadas; no inventes superficie, "
        "disponibilidad de visitas, financiamiento, gastos comunes ni otras propiedades. "
        "Responde en español natural, breve, como el chatbot comercial, con contexto del mensaje previo. "
        "Responde cualquier pregunta libre de forma coherente; si no sabes, dilo. "
        "Un 'sí' genérico significa interés, nunca visita confirmada. "
        "Si desea visitar, pregunta qué día u horario le acomoda sin confirmarlo. "
        "No solicites teléfono, RUT, nombre ni correo. "
        "CRÍTICO: ESTA PRUEBA NO ACTUALIZA CRM, NO ASIGNA LEADS Y NO AVISA A EJECUTIVOS. "
        "Nunca afirmes que se notificó a un ejecutivo, se agendó una visita, se guardó interés "
        "comercial o que alguien contactará al cliente. "
        "Si preguntan por ofertas, indica que un ejecutivo podría evaluarlas, pero no fue informado. "
        "Si rechaza, despídete sin insistir. "
        "Evita mencionar detalles internos salvo que el usuario pregunte."
    )
    messages = [{"role": "system", "content": sys}]
    for item in history[-6:]:
        if item.get("customer_text"):
            messages.append({"role": "user", "content": str(item["customer_text"])[:700]})
        if item.get("bot_text"):
            messages.append({"role": "assistant", "content": str(item["bot_text"])[:700]})
    messages.append({"role": "user", "content": message[:1600]})
    reply = client.chat.completions.create(
        model=Config.DEEPSEEK_MODEL_FAST, messages=messages,
        temperature=Config.DEEPSEEK_TEMPERATURE,
        max_tokens=min(int(Config.DEEPSEEK_MAX_TOKENS_FAST), 300),
        timeout=Config.DEEPSEEK_TIMEOUT_FAST,
    )
    return str(reply.choices[0].message.content or "").strip()


def build_test_reply(message: str, ctx: dict, history: list[dict] | None = None,
                     llm: Callable | None = None) -> str:
    """Common intents deterministic; free-form inquiries use the real LLM provider."""
    clean = str(message or "").strip()
    code = ctx["property_code"]
    new = _format_uf(ctx["new_price_uf"])
    old = _format_uf(ctx["old_price_uf"])
    if not clean:
        return ""
    if _other_property_referenced(clean, code):
        return (f"Esta prueba está asociada al departamento {code}. "
                "Para consultar otra propiedad, debes hacerlo fuera del modo de prueba.")
    if DECLINE.search(clean):
        return "Entendido, gracias por responder. No insistiremos en esta propiedad."
    if VISIT.search(clean):
        return (f"Podemos evaluar una visita al departamento {code}, ahora en UF {new}. "
                "¿Qué día u horario te acomoda? Es una prueba: no se enviará ninguna solicitud a un ejecutivo.")
    if NEGOTIATION.search(clean):
        return (f"El valor publicado del departamento {code} es UF {new}. "
                "Una oferta tendría que revisarla un ejecutivo, pero en esta prueba no la notificaremos ni registraremos.")
    if PRICE.search(clean):
        return f"El departamento {code} bajó de UF {old} a UF {new}. ¿Quieres más información o prefieres coordinar una visita?"
    if YES.search(clean):
        return (f"¡Perfecto! Hablamos del departamento {code} en San Miguel, "
                f"que ahora está en UF {new}. ¿Quieres más información o prefieres coordinar una visita?")
    if ALTERNATIVE.search(clean):
        return ("Podemos buscar otras alternativas, pero esta prueba solo está conectada "
                f"al departamento {code}. No enviaré una consulta a ejecutivos.")
    try:
        reply = (llm or _llm_answer)(clean, ctx, history or [])
        if not reply or BAD_CLAIM.search(reply) or len(reply) > 1800:
            return _safe_fallback(ctx)
        return reply
    except Exception:
        logger.exception("[PRICE_DROP_TEST] model generation unavailable")
        return _safe_fallback(ctx)


def process_test_inbound(db: Any, phone: str, provider_message_id: str, text: str,
                         *, sender: Callable | None = None, llm: Callable | None = None) -> dict:
    """Intercept before commercial queue. All writes confined to test collections.

    One provider attempt per inbound ID. An uncertain send is never retried.
    """
    if not is_test_phone(phone):
        return {"status": "not_test_phone"}
    ctx = load_test_context(db, phone)
    if ctx is None:
        # Fail closed: do not allow this number into the regular CRM while
        # the isolated test flag is active, even if the campaign is expired.
        return {"status": "test_not_active"}
    if not provider_message_id or not text or not str(text).strip():
        return {"status": "test_invalid_inbound"}
    phone_norm = normalize_phone_strict(phone)
    key = hashlib.sha256(f"{phone_norm}|{provider_message_id}".encode()).hexdigest()
    now = datetime.now(timezone.utc)
    try:
        db[TURN_COLLECTION].insert_one({
            "_id": key, "campaign_id": ctx["campaign_id"],
            "customer_text": text[:1600], "provider_inbound_id": str(provider_message_id),
            "created_at": now, "status": "RECEIVED",
        })
    except DuplicateKeyError:
        return {"status": "duplicate_suppressed"}

    if STOP.search(text):
        db[COLLECTION].update_one({"_id": ctx["campaign_id"], "test_mode": True}, {
            "$set": {"status": "STOPPED", "stopped_at": now}
        })
        db[TURN_COLLECTION].update_one({"_id": key}, {"$set": {"status": "STOPPED"}})
        return {"status": "test_stopped"}

    history = list(db[TURN_COLLECTION].find(
        {"campaign_id": ctx["campaign_id"], "status": "SENT", "_id": {"$ne": key}},
        {"customer_text": 1, "bot_text": 1, "created_at": 1},
    ).sort("created_at", -1).limit(6))
    history.reverse()
    answer = build_test_reply(text, ctx, history, llm=llm)
    if not answer:
        db[TURN_COLLECTION].update_one({"_id": key}, {"$set": {"status": "NO_REPLY"}})
        return {"status": "no_reply"}

    db[TURN_COLLECTION].update_one({"_id": key}, {"$set": {
        "status": "SENDING", "bot_text": answer,
    }})
    try:
        if sender is None:
            from .whatsapp_client import send_whatsapp_message_detailed_sync
            sender = send_whatsapp_message_detailed_sync
        receipt = sender(phone_norm, answer)
    except Exception:
        # We might have reached the provider. No retries.
        db[TURN_COLLECTION].update_one({"_id": key}, {"$set": {
            "status": "DELIVERY_UNKNOWN",
        }})
        logger.exception("[PRICE_DROP_TEST] provider outcome uncertain")
        return {"status": "delivery_unknown"}

    accepted = bool(receipt.get("success") and receipt.get("provider_message_id"))
    db[TURN_COLLECTION].update_one({"_id": key}, {"$set": {
        "status": "SENT" if accepted else "DELIVERY_UNKNOWN",
        "provider_outbound_id": receipt.get("provider_message_id"),
        "sent_at": datetime.now(timezone.utc) if accepted else None,
    }})
    return {"status": "test_reply_sent" if accepted else "delivery_unknown"}
