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
VISIT = re.compile(r"\b(?:visitar(?:la|lo)?|visitas?|agendar|coordinar|quiero verla|ir a ver|conocerla|ver el departamento)\b", re.I)
PRICE = re.compile(r"\b(?:precio|cu[aá]nto (?:cuesta|vale|sale)|a cu[aá]nto|rebaja|baj[oó]|uf)\b", re.I)
NEGOTIATION = re.compile(r"\b(?:ofertas?|negociar|rebajar|aceptan menos|descuento|contraofertas?)\b", re.I)
YES = re.compile(r"^\s*(?:s[ií](?:,?\s*me interesa)?|claro|ok(?:a|ey)?(?:\s+me interesa)?|dale|por supuesto|me interesa|perfecto|s[ií]\s+claro)\s*[.!]?\s*$", re.I)
ALTERNATIVE = re.compile(r"\b(?:otra|otras|alternativas|opciones|ñuñoa|nunoa)\b", re.I)
DETAILS = re.compile(r"\b(?:caracter[ií]sticas|detalles|informaci[oó]n del departamento|ficha)\b", re.I)
BATHS = re.compile(r"\b(?:ba[ñn]os?|servicios hig[ií]enicos)\b", re.I)
BEDROOMS = re.compile(r"\b(?:dormitorios?|habitaciones?|piezas?)\b", re.I)
AREA = re.compile(r"\b(?:superficie|metros|m2|m²|tama[ñn]o)\b", re.I)
COMMON_EXPENSES = re.compile(r"\b(?:gastos? comunes?|ggcc)\b", re.I)
STORAGE = re.compile(r"\b(?:bodegas?)\b", re.I)
PARKING = re.compile(r"\b(?:estacionamientos?|parking)\b", re.I)
TERRACE = re.compile(r"\b(?:terrazas?)\b", re.I)
FLOOR = re.compile(r"\b(?:en qu[eé] piso|qu[eé] piso|piso (?:est[aá]|queda)|n[uú]mero de piso)\b", re.I)
ORIENTATION = re.compile(r"\b(?:orientaci[oó]n|hacia d[oó]nde)\b", re.I)
POOL = re.compile(r"\b(?:piscinas?)\b", re.I)
PETS = re.compile(r"\b(?:mascotas?|perros?|gatos?)\b", re.I)
CONTACT_HUMAN = re.compile(r"\b(?:hablar con (?:un |una )?(?:ejecutiv[oa]|asesor[oa])|que me llame|me puede llamar|me pueden llamar|contacte (?:un |una )?(?:ejecutiv[oa]|asesor[oa]))\b", re.I)
NEGATE_VISIT = re.compile(r"\b(?:no quiero (?:visitarla|visitar|visita|una visita|agendar|coordinar)|no me interesa una visita)\b", re.I)
COMMISSION = re.compile(r"\b(?:comisi[oó]n|honorarios|corretaje)\b", re.I)
VISIT_WHEN = re.compile(
    r"\b(?:pr[oó]ximo mes|mes que viene|siguiente mes|pr[oó]xima semana|"
    r"semana que viene|el (?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)|"
    r"(?:ma[ñn]ana|pasado ma[ñn]ana)|por la (?:ma[ñn]ana|tarde)|a fin de mes|"
    r"este fin de semana)\b", re.I
)
BAD_CLAIM = re.compile(
    r"\b(?:he (?:avisado|notificado|contactado|agendado|registrado|derivado)|"
    r"(?:ya |est[aá] )?(?:agendada|confirmada) la visita|"
    r"la visita (?:est[aá] |qued[oó] )?(?:confirmada|agendada)|"
    r"el ejecutivo (?:fue notificado|te (?:llamar[aá]|contactar[aá]))|"
    r"lo (?:dejamos|dej[eé]|dejo|qued[oó]) anotado|"
    r"(?:te vamos a llamar|te contactaremos|ya registr[eé]|queda reservada|qued[oó] agendada|"
    r"le informamos al ejecutivo|avisamos al ejecutivo))\b", re.I
)


def _verified_property_facts(prop: dict) -> dict:
    """Extract only validated Prop360 facts; never infer unknown fields."""
    characteristics = prop.get("caracteristicas") or {}
    operation = prop.get("tipo_operacion") or {}
    facts = {}
    for key in ("banos", "dormitorios", "bodegas"):
        value = characteristics.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 50:
            facts[key] = value
    for key in ("superficie_total", "superficie_util", "superficie_terraza"):
        value = _money(characteristics.get(key))
        if value is not None and value <= 100000:
            facts[key] = value
    for key in ("piso", "ascensores", "ano_construccion"):
        value = characteristics.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            if (1 <= value <= 200 if key != "ano_construccion" else 1800 <= value <= 2100):
                facts[key] = value
    orientation = characteristics.get("orientacion")
    if isinstance(orientation, str) and 1 <= len(orientation.strip()) <= 60:
        facts["orientacion"] = orientation.strip()
    features = characteristics.get("features") or []
    if isinstance(features, list):
        for value, field in (("piscina", "piscina"), ("has_terrace", "terraza"),
                             ("mascotas", "mascotas_ficha")):
            if value in features:
                facts[field] = True
    expenses = _money(operation.get("gastos_comunes"))
    if expenses is not None and expenses < 100000000:
        facts["gastos_comunes_clp"] = int(expenses)
    return facts


def _formatted_facts(facts: dict) -> str:
    labels = (
        ("dormitorios", "dormitorios"),
        ("banos", "baños"),
        ("superficie_total", "m² totales"),
        ("superficie_util", "m² útiles"),
        ("superficie_terraza", "m² de terraza"),
        ("bodegas", "bodega(s)"),
        ("piso", "piso"),
        ("orientacion", "orientación"),
        ("gastos_comunes_clp", "pesos de gastos comunes"),
    )
    items = [f"{facts[key]} {label}" for key, label in labels if key in facts]
    if facts.get("piscina"):
        items.append("piscina según ficha")
    if facts.get("terraza"):
        items.append("terraza según ficha")
    if facts.get("mascotas_ficha"):
        items.append("mascotas mencionadas en ficha (reglamento por confirmar)")
    return ", ".join(items)


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
    if campaign.get("status") != "SENT" or campaign.get("test_mode") is not True:
        return None
    if campaign.get("lead_id") is not None:
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
         "tipo_operacion.venta": 1, "tipo_operacion.precio_venta.precio_uf": 1,
         "tipo_operacion.gastos_comunes": 1, "caracteristicas.banos": 1,
         "caracteristicas.dormitorios": 1, "caracteristicas.bodegas": 1,
         "caracteristicas.superficie_total": 1, "caracteristicas.superficie_util": 1,
         "caracteristicas.superficie_terraza": 1, "caracteristicas.piso": 1,
         "caracteristicas.ascensores": 1, "caracteristicas.ano_construccion": 1,
         "caracteristicas.orientacion": 1, "caracteristicas.features": 1},
    ) or {}
    tipo = property_doc.get("tipo_operacion") or {}
    price = _money((tipo.get("precio_venta") or {}).get("precio_uf"))
    if (str(property_doc.get("codigo") or "") != str(campaign.get("property_code") or "") or
        not property_doc.get("disponible_prop360") or
        (property_doc.get("estado") or {}).get("oficina") != "PROCASA SUCRE" or
        tipo.get("venta") is not True or price is None or abs(price - new) > 0.01):
        return None
    return {
        "campaign_id": campaign["_id"],
        "property_code": str(campaign["property_code"]),
        "old_price_uf": old, "new_price_uf": new,
        "property_facts": _verified_property_facts(property_doc),
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
    facts = _formatted_facts(ctx.get("property_facts") or {})
    sys = (
        "Eres el asistente de PROCASA, en una PRUEBA INTERNA por WhatsApp. "
        f"La propiedad es un departamento en VENTA en San Miguel, código {code}. "
        f"Precio anterior UF {old}, precio ACTUAL UF {price}. "
        f"Datos contrastados con Prop360: {facts or 'sin atributos adicionales confirmados'}. "
        "Usa exclusivamente estos hechos. No inventes otros atributos, disponibilidad "
        "de visitas, financiamiento ni otras propiedades. "
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
    if NEGATE_VISIT.search(clean):
        return (f"De acuerdo, no coordinaremos visitas al departamento {code}. "
                "Si prefieres, puedes consultar características o el precio sin compromiso.")
    if DECLINE.search(clean):
        return "Entendido, gracias por responder. No insistiremos en esta propiedad."
    facts = ctx.get("property_facts") or {}
    previous = (history or [])[-1].get("bot_text", "") if history else ""
    if VISIT_WHEN.search(clean) and re.search(r"d[ií]a|horario|semana", previous, re.I):
        return (f"Entiendo, considerarías visitar el departamento {code} {clean.lower()}. "
                "¿Tienes un día u horario más concreto en mente? "
                "Es una prueba: no se ha agendado ninguna visita ni notificado a un ejecutivo.")
    if DETAILS.search(clean) and facts:
        return (f"El departamento {code} está en venta a UF {new}. "
                f"Su ficha informa: {_formatted_facts(facts)}. "
                "¿Hay alguna característica que quieras conocer con más detalle?")
    # Multiple questions in one message should yield all verified attributes.
    checks = (
        (BATHS, "banos", lambda v: f"{v} baños"),
        (BEDROOMS, "dormitorios", lambda v: f"{v} dormitorios"),
        (COMMON_EXPENSES, "gastos_comunes_clp", lambda v: f"${v:,.0f}".replace(",", ".") + " de gastos comunes mensuales"),
        (AREA, "superficie_total", lambda v: f"{v:g} m² totales"),
        (STORAGE, "bodegas", lambda v: f"{v} bodega(s)"),
        (PARKING, "estacionamientos", lambda v: f"{v} estacionamiento(s)"),
        (TERRACE, "superficie_terraza", lambda v: f"{v:g} m² de terraza"),
        (FLOOR, "piso", lambda v: f"piso {v}"),
        (ORIENTATION, "orientacion", lambda v: f"orientación {v}"),
        (POOL, "piscina", lambda v: "piscina informada en la ficha"),
    )
    asked = [(key, formatter) for pattern, key, formatter in checks if pattern.search(clean)]
    if asked:
        found = [format_value(facts[key]) for key, format_value in asked if key in facts]
        missing = [key for key, _ in asked if key not in facts]
        answer = (f"Según la ficha vigente del departamento {code}: " + ", ".join(found) + ".") if found else ""
        if missing:
            answer += (" " if answer else "") + "No tengo confirmado " + (
                "el resto de esos datos." if found else "ese dato en su ficha."
            )
        if PRICE.search(clean):
            answer += f" El precio vigente es UF {new}."
        return answer
    if PETS.search(clean):
        return (f"La ficha del departamento {code} menciona mascotas, pero el reglamento del edificio "
                "y sus condiciones deben confirmarse." if facts.get("mascotas_ficha") else
                f"No tengo confirmadas las reglas sobre mascotas del departamento {code}.")
    if COMMISSION.search(clean):
        return "No tengo confirmados los honorarios de corretaje para esta operación."
    if CONTACT_HUMAN.search(clean):
        return ("Puedes pedir hablar con un ejecutivo, pero en esta prueba no se enviará "
                "ninguna solicitud ni notificación comercial.")
    if VISIT.search(clean):
        if VISIT_WHEN.search(clean):
            return (f"Entiendo que quieres evaluar una visita al departamento {code} {clean.lower()}. "
                    "¿Qué horario te acomodaría? Es una prueba: ninguna visita quedó agendada "
                    "ni se notificó a un ejecutivo.")
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
