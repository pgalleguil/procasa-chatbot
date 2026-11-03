"""Zero-CRM side-effect WhatsApp smoke test for the price-drop campaign.

This module is **not** the normal lead handler. Only the explicitly enabled
test phone is intercepted at the webhook boundary, before event_log,
commercial_intake, queue, CRM/SLA, chatbot.core, and notification services.
The separate collections are private to this isolated test.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
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
ADDRESS = re.compile(r"\b(?:d[oó]nde (?:queda|quedaba|est[aá]|se ubica|se encuentra)|ubicaci[oó]n|direcci[oó]n|calle|en qu[eé] parte)\b", re.I)
GRILL = re.compile(r"\b(?:quinchos?|parrillas?|asados?)\b", re.I)
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
    r"le informamos al ejecutivo|avisamos al ejecutivo|queda (?:solo )?registrad[oa]|qued[oó] registrad[oa]|quedo registrado|lo registramos|anot[eé] tu preferencia))\b", re.I
)


# Keep these helpers pure: visit preferences exist only in this test thread.
_GREETING = re.compile(r"^\s*(?:hola+|holi|buen(?:os|as) (?:d[ií]as|tardes|noches)|hello|buenas)[!¡.,\s]*$", re.I)
_VISIT_DAY = re.compile(r"\b(lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b")
_VISIT_MORNING = re.compile(r"\b(?:en|por)\s+la\s+manana\b|\b(?:durante|tipo)\s+la\s+manana\b")
_VISIT_AFTERNOON = re.compile(r"\b(?:en|por)\s+la\s+tarde\b")
_VISIT_EVENING = re.compile(r"\b(?:en|por)\s+la\s+noche\b")
_VISIT_TIME = re.compile(r"\b(?:a\s+las|tipo|cerca\s+de\s+las)\s+([0-2]?\d(?::[0-5]\d)?)\b")
_VISIT_TWO_TIMES = re.compile(r"\b(?:a\s+las|tipo|cerca\s+de\s+las)\s+([0-2]?\d(?::[0-5]\d)?)\s+(?:o|u)\s+([0-2]?\d(?::[0-5]\d)?)\b")
_VISIT_CLARIFY = re.compile(r"^\s*(?:que|como|perdon|no entiendo|no cache|a que te refieres)[?.!¿¡\s]*$")
_VISIT_AFFIRM = re.compile(r"^\s*(?:si+|sip|claro|correcto|ok|okay|dale)[.!?\s]*$")
_VISIT_CONTACT = re.compile(r"\b(?:me contactaran|me llamaran|me van a contactar|me van a llamar|se comunicaran conmigo)\b")
_VISIT_FOLLOWUP = re.compile(r"\b(?:ya te respondi|ya te dije|te acabo de decir|ya lo dije|te dije)\b")
_VISIT_PROMPT = re.compile(r"\b(?:que dia|que horario|dia u horario|dia o horario|a que hora|horario te|dia te)\b")
# Only introductions from the customer: never mine names of relatives or agents.
_NAME_INTRO = re.compile(
    r"^\s*(?:(?:hola|holi|buenas|buenos d[ií]as|buenas tardes)[\s,!¡.:-]+|no[,;]\s*)?"
    r"(?:yo\s+)?(?:me llamo|mi nombre es|soy|habla)\s+"
    r"([a-záéíóúüñ]{2,24})(?:\s+([a-záéíóúüñ]{2,24}))?",
    re.I,
)
_NAME_QUESTION = re.compile(
    r"\b(?:como me llamo|cual es mi nombre|recuerdas mi nombre|te acuerdas de mi nombre)\b"
)
_NAME_NONPERSON = {
    "de", "del", "el", "la", "un", "una", "cliente", "comprador",
    "compradora", "interesado", "interesada", "propietario", "propietaria",
    "quien", "persona", "nuevo", "nueva", "bien", "alguien",
}
_COMPOUND_FIRST_NAMES = {
    "maria": {"jose", "paz", "isabel", "fernanda"},
    "juan": {"pablo", "carlos", "jose"},
    "jose": {"luis", "miguel", "antonio"},
    "ana": {"maria", "paula", "sofia"},
}


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").casefold())
                   if not unicodedata.combining(c))


def _outreach_origin_challenge(message: str) -> bool:
    """Recognize when the recipient calls out that PROCASA initiated contact.

    Tolerate common mobile typos ('qye', 'escrobiendo') while avoiding a
    broad sentiment classifier that could mislabel this as distrust.
    """
    clean = re.sub(r"\bqye\b", "que", _plain(message))
    outbound_verb = re.search(
        r"\b(?:escrib\w*|escrob\w*|contact\w*|mand\w*|envi\w*)\b", clean
    )
    sender_reference = re.search(r"\b(?:tu|ustedes|procasa)\b", clean)
    objection_to_question = re.search(
        r"\b(?:como que|por que)\b.{0,65}\b"
        r"(?:me gustaria saber|me preguntas|que quiero saber)\b", clean
    )
    return bool((outbound_verb and sender_reference) or objection_to_question)


def _valid_first_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-záéíóúüñ]{2,24}(?: [a-záéíóúüñ]{2,24})?", name, re.I)
                and _plain(name.split()[0]) not in _NAME_NONPERSON)


def _customer_introduction(message: str) -> tuple[str | None, bool]:
    """Return first name only when the CUSTOMER explicitly introduces themself."""
    match = _NAME_INTRO.match(str(message or ""))
    if not match:
        return None, False
    first, second = match.group(1), match.group(2)
    if not _valid_first_name(first):
        return None, False
    name = first[0].upper() + first[1:].lower()
    if second and _plain(first) in _COMPOUND_FIRST_NAMES:
        if _plain(second) in _COMPOUND_FIRST_NAMES[_plain(first)]:
            name += " " + second[0].upper() + second[1:].lower()
    remainder = str(message or "")[match.end():].strip(" \t\r\n,;.!?¡¿")
    # The optional second token might be a surname or the word 'y'.
    return name, not remainder or remainder == "y"


def _personalize_reply(reply: str, message: str, ctx: dict,
                       history: list[dict]) -> str:
    """Use a confirmed name occasionally, never in every short reply."""
    name = ctx.get("customer_first_name")
    if not name or not _valid_first_name(name) or DECLINE.search(message):
        return reply
    if re.search(r"\b" + re.escape(name) + r"\b", reply, re.I):
        return reply
    introduced, _ = _customer_introduction(message)
    if not introduced and any(re.search(r"\b" + re.escape(name) + r"\b",
                                        str(t.get("bot_text") or ""), re.I)
                              for t in history[-2:]):
        return reply
    for start, replacement in (
        ("¡Hola de nuevo!", f"¡Hola de nuevo, {name}!"),
        ("¡Hola!", f"¡Hola, {name}!"),
        ("¡Claro!", f"¡Claro, {name}!"),
        ("¡Qué bueno!", f"¡Qué bueno, {name}!"),
        ("Perfecto, ", f"Perfecto, {name}, "),
        ("Sí, ", f"Sí, {name}, "),
    ):
        if reply.startswith(start):
            return replacement + reply[len(start):]
    return reply


def _visit_parts(message: str) -> dict:
    """Extract a user's stated preferences, not an appointment or CRM event."""
    plain = _plain(message)
    parts: dict = {}
    day = _VISIT_DAY.search(plain)
    if day:
        weekday = day.group(1)
        earlier = plain[max(0, day.start() - 20):day.start()]
        next_word = re.search(r"(?:proximo|prroximo|siguiente|este)\s*$", earlier)
        parts["day"] = (
            "el próximo " + weekday if next_word and next_word.group().strip() in ("proximo", "prroximo")
            else "el " + weekday
        )
    elif re.search(r"\bpasado manana\b", plain):
        parts["day"] = "pasado mañana"
    elif re.search(r"\b(?:manana|mnana)\b", _VISIT_MORNING.sub("", plain)):
        # "Mñana" is a common phone-keyboard typo for "mañana". Do not
        # confuse "por la mañana" (daypart) with the following day.
        parts["day"] = "mañana"
    elif re.search(r"\b(?:proximo mes|mes que viene|siguiente mes)\b", plain):
        parts["period"] = "el próximo mes"
    elif re.search(r"\b(?:proxima semana|semana que viene)\b", plain):
        parts["period"] = "la próxima semana"
    elif re.search(r"\beste fin de semana\b", plain):
        parts["period"] = "este fin de semana"
    if _VISIT_MORNING.search(plain):
        parts["time"] = "por la mañana"
    elif _VISIT_AFTERNOON.search(plain):
        parts["time"] = "por la tarde"
    elif _VISIT_EVENING.search(plain):
        parts["time"] = "por la noche"
    two_times = _VISIT_TWO_TIMES.search(plain)
    clock = _VISIT_TIME.search(plain)
    if two_times:
        parts["time"] = "a las " + two_times.group(1) + " o " + two_times.group(2)
    elif clock:
        parts["time"] = "a las " + clock.group(1)
    return parts


def _visit_reply(message: str, ctx: dict, history: list[dict]) -> str | None:
    """Use only recent customer turns in the active visit thread; never book."""
    recent = []
    anchored = False
    for item in reversed(history[-12:]):
        prior = str(item.get("customer_text") or "")
        parsed = _visit_parts(prior)
        if NEGATE_VISIT.search(prior) or DECLINE.search(prior) or STOP.search(prior):
            break
        if VISIT.search(prior):
            recent.append(prior)
            anchored = True
            break
        prior_plain = _plain(prior)
        if (parsed or _VISIT_FOLLOWUP.search(prior_plain) or
            _VISIT_CLARIFY.fullmatch(prior_plain) or
            _VISIT_AFFIRM.fullmatch(prior_plain) or
            _VISIT_CONTACT.search(prior_plain)):
            recent.append(prior)
        elif prior:
            break
    # A direct visit request opens a new thread. A price enquiry does not.
    direct_visit = bool(VISIT.search(message) and not NEGATE_VISIT.search(message))
    previous_bot = str(history[-1].get("bot_text") or "") if history else ""
    bot_prompt = bool(_VISIT_PROMPT.search(_plain(previous_bot))
                      and re.search(r"visita|conocerlo|verla|departamento", _plain(previous_bot)))
    current = _visit_parts(message)
    active = direct_visit or anchored or (bool(current) and bot_prompt)
    if not active:
        return None
    clean_plain = _plain(message)
    if not (direct_visit or current or _VISIT_FOLLOWUP.search(clean_plain)
            or _VISIT_CLARIFY.fullmatch(clean_plain)
            or _VISIT_AFFIRM.fullmatch(clean_plain)):
        return None
    choice: dict = {}
    for turn in reversed(recent):
        fields = _visit_parts(turn)
        for key, value in fields.items():
            if key == "day" and value.startswith("el ") and not value.startswith("el próximo "):
                if choice.get("day") == "el próximo " + value[3:]:
                    continue
            choice[key] = value
    for key, value in current.items():
        if key == "day" and value.startswith("el ") and not value.startswith("el próximo "):
            if choice.get("day") == "el próximo " + value[3:]:
                continue
        choice[key] = value
    day = choice.get("day")
    time = choice.get("time")
    period = choice.get("period")
    code = ctx["property_code"]
    if day and time:
        intro = "Sí, entendí: " if (_VISIT_CLARIFY.fullmatch(clean_plain)
                                   or _VISIT_FOLLOWUP.search(clean_plain)
                                   or _VISIT_AFFIRM.fullmatch(clean_plain)) else "Perfecto, "
        return (f"{intro}te acomodaría {day} {time} para conocer el departamento {code} 😊. "
                "Todavía no hay una visita agendada ni se notificó a un ejecutivo.")
    if day:
        return (f"Perfecto, {day} para conocer el departamento {code}. "
                "¿Te acomoda más en la mañana o en la tarde? "
                "Todavía no hay una visita agendada.")
    if time:
        return (f"Entiendo, prefieres {time} para conocer el departamento {code}. "
                "¿Qué día te acomodaría? Todavía no hay una visita agendada.")
    if period:
        return (f"De acuerdo, te interesaría visitar el departamento {code} {period}. "
                "¿Tienes algún día en mente? Todavía no hay una visita agendada.")
    return (f"¡Claro! Podemos conversar sobre una visita al departamento {code}. "
            "¿Qué día te acomodaría? En esta prueba no se agenda ni se notifica a un ejecutivo.")


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
                             ("mascotas", "mascotas_ficha"), ("has_grill", "parrillas_ficha")):
            if value in features:
                facts[field] = True
    location = prop.get("ubicacion") or {}
    if isinstance(location, dict):
        # Prop360's own published street field; never guess missing unit/number.
        for source, target in (("calle", "calle_publicada"),
                               ("direccion_referencial", "direccion_referencial"),
                               ("comuna", "comuna")):
            value = location.get(source)
            if (isinstance(value, str) and 2 <= len(value.strip()) <= 120
                and not any(ch in value for ch in ("\n", "\r", "<", ">"))):
                facts[target] = value.strip()
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
    if facts.get("parrillas_ficha"):
        items.append("parrillas mencionadas en ficha (detalles de quinchos por confirmar)")
    if facts.get("calle_publicada"):
        items.append(f"dirección publicada: {facts['calle_publicada']}")
    elif facts.get("direccion_referencial"):
        items.append(f"referencia de ubicación: {facts['direccion_referencial']}")
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
         "caracteristicas.orientacion": 1, "caracteristicas.features": 1,
         "ubicacion.calle": 1, "ubicacion.direccion_referencial": 1,
         "ubicacion.comuna": 1},
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
        "customer_first_name": (
            campaign.get("customer_first_name")
            if campaign.get("customer_first_name_source") == "customer_message"
            and isinstance(campaign.get("customer_first_name"), str)
            and _valid_first_name(campaign["customer_first_name"])
            else None
        ),
    }


def _safe_fallback(ctx: dict) -> str:
    return (
        "Disculpa, no quiero responderte algo incorrecto. "
        "¿Me puedes explicar un poquito mejor qué necesitas saber?"
        " En esta conversación no se ha solicitado ninguna gestión a un ejecutivo."
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
        "Eres una ejecutiva virtual de PROCASA en WhatsApp. Atiendes con el estilo "
        "del asistente comercial habitual: cálida, cercana, precisa y genuina, "
        "en español chileno profesional sin chilenismos forzados. "
        "Es una continuación de una comunicación informativa por rebaja de precio, "
        "NO el primer contacto; nunca pidas de nuevo un enlace, código o teléfono. "
        "PROCASA ES QUIEN INICIÓ LA CONVERSACIÓN: enviamos primero el mensaje "
        "de rebaja y el cliente está RESPONDIENDO. Jamás lo trates como si "
        "hubiera iniciado él la consulta. El mensaje inicial ya incluyó "
        "precio anterior, precio nuevo, enlace y la pregunta sobre el nuevo valor. "
        "No repitas el texto inicial ni preguntes genéricamente qué quiere saber. "
        "Si nos reclama que fuimos nosotros quienes escribimos primero, "
        "reconoce el error y discúlpate, sin inferir desconfianza. "
        f"Propiedad: departamento en venta de San Miguel, código {code}. "
        f"Precio ANTERIOR UF {old}; precio VIGENTE UF {price}, rebaja confirmada. "
        f"ÚNICOS datos verificados en Prop360: {facts or 'sin atributos adicionales confirmados'}. "
        "Prioriza lo que la persona acaba de preguntar y el historial previo; "
        "no repitas saludos, el código, la rebaja ni una lista de características "
        "en cada respuesta. Responde normalmente en 1-3 frases y máximo 100 palabras, "
        "sin encabezados, sin bullets innecesarios y sin terminar frases a medias. "
        "Puedes usar un emoji ocasional, no en todos los mensajes. "
        "Si hay un dato verificado, dilo directamente y con naturalidad. "
        "Si falta, dilo con honestidad y ofrece confirmarlo más adelante; "
        "no supongas dirección, quinchos, instalaciones, disponibilidad, "
        "financiamiento, ofertas ni otros inmuebles. "
        "No atribuyas al cliente nombres, comentarios ni conversaciones que no figuren "
        "expresamente en el historial confiable. "
        f"Nombre confirmado por presentación explícita del propio cliente: "
        f"{ctx.get('customer_first_name') or 'ninguno'}. "
        "Si existe ese nombre, úsalo con naturalidad en momentos importantes, "
        "como el saludo o una solicitud de visita, pero nunca en cada mensaje. "
        "Si no existe, no inventes ni solicites uno. "
        "Un 'sí' ambiguo indica interés general, NO una confirmación de visita. "
        "Para visitas, pregunta solo el dato temporal faltante y recuerda los ya dichos. "
        "Nunca confirmes un horario, agenda, reserva, contacto ni aviso a un ejecutivo. "
        "IMPORTANTE: este teléfono es una prueba aislada y NO se escriben leads ni CRM, "
        "NO se notifica ejecutivos, NO se agenda visita, NO se inicia gestión comercial. "
        "No expliques la infraestructura ni menciones que es una prueba salvo cuando "
        "sea necesario aclarar expresamente que una acción no ha sido realizada. "
        "Si solicita que lo contacten, responde claramente que aún no se ha solicitado "
        "contacto. Si rechaza, respeta su decisión sin insistir. "
        "No solicites RUT, nombre, correo, teléfono ni otros datos personales."
    )
    messages = [
        {"role": "system", "content": sys},
        {"role": "assistant", "content": (
            "Hola, ¿cómo estás? Soy de PROCASA 😊. Te escribí porque el "
            f"departamento de San Miguel bajó de UF {old} a UF {price}. "
            f"Te compartí https://www.procasa.cl/{code}. "
            "¿Cómo lo ves con este nuevo valor?"
        )},
    ]
    for item in history[-10:]:
        if item.get("customer_text"):
            messages.append({"role": "user", "content": str(item["customer_text"])[:700]})
        if item.get("bot_text"):
            messages.append({"role": "assistant", "content": str(item["bot_text"])[:700]})
    messages.append({"role": "user", "content": message[:1600]})
    reply = client.chat.completions.create(
        model=Config.DEEPSEEK_MODEL_FAST, messages=messages,
        temperature=Config.DEEPSEEK_TEMPERATURE,
        max_tokens=min(int(Config.DEEPSEEK_MAX_TOKENS_FAST), 550),
        timeout=Config.DEEPSEEK_TIMEOUT_FAST,
    )
    candidate = reply.choices[0]
    # Avoid sending the incomplete sentence observed in the November 2 test.
    if getattr(candidate, "finish_reason", None) == "length":
        logger.warning("[PRICE_DROP_TEST] model reply truncated at token limit")
        return ""
    answer = str(candidate.message.content or "").strip()
    if not answer or (len(answer) > 1200):
        return ""
    return answer


# The native model is opt-in and can only run inside the pre-existing
# single-phone isolated webhook branch. It never calls chatbot.core (which
# persists CRM messages, lead fields and commercial notifications).
NATIVE_PILOT_ENGINE_FLAG = "PRICE_DROP_PILOT_NATIVE_CONVERSATION"
_CAMPAIGN_SENSITIVE_REQUEST = re.compile(
    r"\b(?:enviame|env[ií]ame|mandame|m[aá]ndame|comp[aá]rteme)\b.{0,35}"
    r"\b(?:enlace|link|c[oó]digo|telefono|tel[eé]fono|rut|correo)\b",
    re.I,
)
_CAMPAIGN_UNVERIFIED_ACTION = re.compile(
    r"\b(?:te (?:agend[eé]|reserv[eé]|inscrib[ií]|registr[eé])|"
    r"tu visita (?:qued[oó]|est[aá])|"
    r"un ejecutivo (?:ya |te )?(?:avis[oó]|llamar[aá]|contactar[aá]))\b",
    re.I,
)


def _native_campaign_enabled() -> bool:
    return os.getenv(NATIVE_PILOT_ENGINE_FLAG, "").strip().lower() in {
        "1", "true", "yes"
    }


def _build_native_campaign_messages(message: str, ctx: dict,
                                    history: list[dict]) -> list[dict]:
    """Prompt payload with the REAL outreach as an assistant turn.

    No lead database is read. An earlier property inquiry is never claimed
    without an independently verified source in an eventual real campaign.
    """
    old = _format_uf(ctx["old_price_uf"])
    new = _format_uf(ctx["new_price_uf"])
    discount_uf = ctx["old_price_uf"] - ctx["new_price_uf"]
    discount_pct = discount_uf / ctx["old_price_uf"] * 100
    code = str(ctx["property_code"])
    facts = _formatted_facts(ctx.get("property_facts") or {})
    name = ctx.get("customer_first_name")
    # No CRM/lead identity lookup is permitted for the isolated test.
    known_name = name if isinstance(name, str) and _valid_first_name(name) else None
    official = (
        "[DATOS OFICIALES DE LA PROPIEDAD ACTIVA]\\n"
        f"Venta de departamento en San Miguel, código {code}; "
        f"precio vigente UF {new}; rebaja verificada de UF {old} a UF {new} "
        f"({discount_uf:g} UF, {discount_pct:.2f}% sobre precio anterior). "
        f"Ficha Prop360: {facts or 'sin otros atributos confirmados'}. "
        f"URL comprobable: https://www.procasa.cl/{code}."
    )
    context = (
        "[CONTEXTO COMERCIAL DEL MENSAJE SALIENTE - PRIORIDAD]\\n"
        "Continúa la conversación de un ejecutivo inmobiliario PROCASA. "
        "PROCASA, no el cliente, inició este contacto: acabamos de enviar "
        "el aviso de rebaja que figura a continuación como un mensaje assistant. "
        "El cliente está respondiendo a ESE mensaje. "
        "Usa las mismas reglas, el tono comercial y la capacidad conversacional "
        "del chatbot normal de leads; la única diferencia es que ya conoce la "
        "propiedad, la rebaja y el motivo del contacto. "
        "No vuelvas a pedir enlace, código o teléfono ni a presentarte como si "
        "el cliente hubiera escrito por primera vez. No repitas la rebaja ni "
        "preguntes de forma mecánica 'qué te gustaría saber' ante un saludo. "
        "Si el cliente cuestiona que le preguntemos, reconoce que nosotros "
        "escribimos primero; no lo confundas con desconfianza. "
        "Responde primero a lo que acaba de expresar: reconoce objeciones, "
        "responde datos verificables, usa el nombre confirmado con moderación, "
        "y conversa con naturalidad sin llenar cada turno de preguntas. "
        "Mantén la continuidad de interés, dudas y horarios ya mencionados. "
        "En lo habitual, 1-3 frases y menos de 80 palabras; sin fichas largas "
        "ni repetir códigos, precios, saludos o enlaces innecesariamente. "
        "No presupongas que esta persona consultó días/semanas atrás: en "
        "ESTE test aislado no hay historial CRM verificado. "
        "En campañas reales solo se puede referir a consultas previas cuando "
        "exista trazabilidad suficiente de esa misma propiedad y lead. "
        f"Nombre confirmado por el propio cliente: {known_name or 'no informado'}. "
        "No inventes nombres ni detalles personales. "
        "NO solicites RUT, nombre, correo ni teléfono; acepta el nombre si lo "
        "entrega espontáneamente. Si quiere una visita, puedes preguntar "
        "día u horario faltante, pero no confirmes disponibilidad ni digas que "
        "se registró, notificó, agendó o transfirió nada. "
        "Esta prueba NO contacta ejecutivos ni modifica CRM. "
        "No menciones detalles técnicos de la prueba salvo que debas aclarar "
        "que una gestión real no ha ocurrido. "
        "Respeta el rechazo y las solicitudes de no recibir mensajes.\\n"
        + official
    )
    # The campaign was actually sent once, so its text acts as the same
    # assistant history that the normal chatbot would use for a first lead.
    outbound = (
        "Hola, ¿cómo estás? Soy de PROCASA 😊\\n\\n"
        "Te escribo porque el departamento de San Miguel tuvo una rebaja: "
        f"ahora está en UF {new}, {discount_uf:g} UF menos que antes "
        f"(UF {old}).\\n\\n"
        "Con este nuevo precio quizás valga la pena darle otra mirada. "
        "Acá está la publicación actualizada:\\n"
        f"https://www.procasa.cl/{code}\\n\\n"
        "¿Cómo lo ves con este nuevo valor?"
    )
    from .prompts import SYSTEM_PROMPT_PROSPECTO
    result = [
        # Same commercial personality/system instructions as the normal
        # chatbot in chatbot.core; the addendum only changes outreach origin.
        {"role": "system", "content": SYSTEM_PROMPT_PROSPECTO},
        {"role": "system", "content": context},
        {"role": "assistant", "content": outbound},
    ]
    for item in history[-12:]:
        if item.get("customer_text"):
            result.append({"role": "user",
                           "content": str(item["customer_text"])[:1000]})
        if item.get("bot_text"):
            result.append({"role": "assistant",
                           "content": str(item["bot_text"])[:1000]})
    result.append({"role": "user", "content": str(message)[:1600]})
    return result


def build_native_pilot_reply(message: str, ctx: dict, history: list[dict],
                             *, generator: Callable | None = None) -> str:
    """Run the SAME structured commercial DeepSeek generator as normal leads.

    Only a safe, explicit campaign context changes. Extracted prospect fields
    are intentionally discarded; the pilot does not call CRM operations.
    """
    clean = str(message or "").strip()
    if not clean:
        return ""
    code = str(ctx["property_code"])
    if _other_property_referenced(clean, code):
        return (f"Esta conversación es sobre el departamento {code}. "
                "Podemos hablar de otra propiedad, pero no tengo su ficha verificada aquí.")
    if NEGATE_VISIT.search(clean):
        return ("Entendido, no coordinaremos una visita. "
                "Si quieres conversar sobre la propiedad, encantada.")
    if DECLINE.search(clean) and not _customer_introduction(clean)[0]:
        return "Entiendo, gracias por responder. No volveremos a insistir por esta propiedad."
    if _NAME_QUESTION.search(_plain(clean)):
        name = ctx.get("customer_first_name")
        return (f"Me dijiste que te llamas {name} 😊." if name
                else "Todavía no me has indicado tu nombre.")
    from .grok_client import generar_respuesta_estructurada
    generate = generator or generar_respuesta_estructurada
    messages = _build_native_campaign_messages(clean, ctx, history)
    try:
        result = generate(
            messages,
            {"operacion": "Venta"},  # No phone or lead ID: no CRM lookups.
            write_telemetry=False,   # No observability DB writes either.
        )
        answer = str((result or {}).get("respuesta_bot") or "").strip()
        if (not answer or len(answer) > 650 or BAD_CLAIM.search(answer)
                or _CAMPAIGN_UNVERIFIED_ACTION.search(answer)
                or _CAMPAIGN_SENSITIVE_REQUEST.search(answer)):
            return _safe_fallback(ctx)
        return answer
    except Exception:
        logger.exception("[PRICE_DROP_TEST] native commercial generator unavailable")
        return _safe_fallback(ctx)


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
    # "No, me llamo Andrés" is a name correction, not a rejection.
    introduction, only_introduction = _customer_introduction(clean)
    if introduction and only_introduction:
        return (f"¡Mucho gusto, {introduction}! 😊 "
                "¿Qué te gustaría saber del departamento?")
    if DECLINE.search(clean):
        return "Entendido, gracias por responder. No insistiremos en esta propiedad."
    # The commercial conversation started with OUR campaign, not with
    # this customer's first inbound. Do not classify their objection as fear.
    if _outreach_origin_challenge(clean):
        return (
            "Tienes toda la razón, fuimos nosotros quienes te escribimos. "
            "Disculpa, te respondí como si tú hubieras iniciado la conversación. "
            "Solo queríamos comentarte la rebaja del departamento; "
            "si te interesa conversarlo, encantada, y si no, ningún problema."
        )
    facts = ctx.get("property_facts") or {}
    if _NAME_QUESTION.search(_plain(clean)):
        name = ctx.get("customer_first_name") or introduction
        return (f"Me dijiste que te llamas {name} 😊." if name
                else "Todavía no me has indicado tu nombre.")
    # Visit preference is reconstructed from customer turns, never written to CRM.
    visit_reply = _visit_reply(clean, ctx, history or [])
    if visit_reply is not None:
        return visit_reply
    if _GREETING.fullmatch(clean):
        if history:
            return "¡Hola de nuevo! 😊 Te leo."
        return (
            "¡Hola! 😊 Gracias por responder. "
            "Te escribí por la rebaja del departamento de San Miguel. "
            "¿Alcanzaste a revisar la publicación?"
        )
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
    if asked or ADDRESS.search(clean) or GRILL.search(clean):
        found = [format_value(facts[key]) for key, format_value in asked if key in facts]
        missing = [key for key, _ in asked if key not in facts]
        if ADDRESS.search(clean):
            address = facts.get("calle_publicada")
            ref = facts.get("direccion_referencial")
            if address:
                position = f"queda en {address}"
                if facts.get("comuna"):
                    position += f", {facts['comuna']}"
                if ref:
                    position += f" (referencia: {ref})"
                found.append(position)
            elif ref:
                found.append(f"referencia de ubicación: {ref}, {facts.get('comuna', 'San Miguel')}")
            else:
                found.append(f"ubicación: {facts.get('comuna', 'San Miguel')}; dirección exacta no confirmada")
        if GRILL.search(clean):
            if facts.get("parrillas_ficha"):
                found.append("hay parrillas mencionadas en la ficha; cantidad y condiciones de uso de quinchos por confirmar")
            else:
                found.append("no hay confirmación de quinchos o parrillas en la ficha")
        # Answers are short and customer-facing, not an inventory export.
        if found:
            answer = ("" if found[0].startswith("no hay confirmación") else "Sí, ") + ", ".join(found) + "."
        else:
            answer = ""
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
    if _VISIT_CONTACT.search(_plain(clean)):
        return ("En esta prueba no se notifica a ningún ejecutivo ni se solicita contacto. "
                "Podemos conversar sobre la visita, pero no ha quedado agendada.")
    if CONTACT_HUMAN.search(clean):
        return ("Puedes pedir hablar con un ejecutivo, pero en esta prueba no se enviará "
                "ninguna solicitud ni notificación comercial.")
    if VISIT.search(clean):
        if VISIT_WHEN.search(clean):
            return (f"¡Claro! ¿Qué horario te acomodaría para conocer el departamento {code}? "
                    "Aún no hay una visita agendada ni avisamos a un ejecutivo.")
        return (f"¡Claro! ¿Qué día te acomodaría para visitar el departamento {code}? "
                "Por ahora no hay una visita agendada ni hemos avisado a un ejecutivo.")
    if NEGOTIATION.search(clean):
        return (f"El valor publicado es UF {new}. Puedes proponer una oferta, "
                "pero tendría que revisarla un ejecutivo. Desde esta conversación "
                "de prueba todavía no podemos enviársela.")
    if PRICE.search(clean):
        return (f"Sí, ahora está en UF {new}; antes costaba UF {old}. "
                "¿Te interesa saber algo más del departamento?")
    if YES.search(clean):
        return (
            "¡Qué bueno! 😊 Si quieres, podemos revisar tus dudas sobre "
            "el departamento o conversar sobre una posible visita."
        )
    if ALTERNATIVE.search(clean):
        return ("Podemos buscar otras alternativas, pero esta prueba solo está conectada "
                f"al departamento {code}. No enviaré una consulta a ejecutivos.")
    try:
        reply = (llm or _llm_answer)(clean, ctx, history or [])
        if not reply or BAD_CLAIM.search(reply) or len(reply) > 1200:
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
    ).sort("created_at", -1).limit(12))
    history.reverse()
    # Save ONLY the customer's explicitly provided first name in this isolated
    # test campaign, not the production CRM; it outlives the 12-turn window.
    introduced, _ = _customer_introduction(text)
    if introduced and introduced != ctx.get("customer_first_name"):
        db[COLLECTION].update_one({"_id": ctx["campaign_id"], "test_mode": True}, {
            "$set": {"customer_first_name": introduced,
                     "customer_first_name_source": "customer_message",
                     "customer_first_name_updated_at": now}
        })
        ctx["customer_first_name"] = introduced
    # The shared commercial generator is available ONLY for this whitelisted
    # test phone. Existing legacy pilot behavior remains the safe default
    # until the dedicated natural-conversation switch is explicitly enabled.
    if _native_campaign_enabled() and llm is None:
        response = build_native_pilot_reply(text, ctx, history)
    else:
        response = build_test_reply(text, ctx, history, llm=llm)
    answer = _personalize_reply(response, text, ctx, history)
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
