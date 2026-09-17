"""Grounded, deterministic Phase 3 conversation policy.

This module intentionally separates state, next-best-action and safe response
construction from model generation.  It contains no I/O and never confirms a
visit, CRM operation, notification or executive contact without evidence.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from chatbot.conversation_policy import (
    build_property_search_response,
    contains_property_identifier,
    extract_search_criteria,
    is_property_search_intent,
)

POLICY_VERSION = "conversation_policy_v3_1"
PROMPT_VERSION = "prompt_phase3_grounded_nba_v2"
ACTION_VALUES = {"ANSWER_ONLY", "ASK_CLARIFICATION", "ASK_QUALIFICATION", "PROPOSE_VISIT", "HANDOFF_HUMAN", "WAIT_CUSTOMER", "SEARCH_PROPERTY"}

_VISIT = re.compile(r"\b(?:quiero\s+(?:verla|visitarla)|puedo\s+(?:ir|ver(?:la)?|visitar(?:la)?)|cu[aá]ndo\s+la\s+puedo\s+ver|coordinar|agendar|visita(?:r)?|verla|verlo|res[eé]rv|(?:me|mw)\s+gustar[ií]a\s+(?:visitarla|verla)|kiero\s+verla|quiero\s+visistarla|se\s+pued(?:e)?\s+visitar|conoserla)\b", re.I)
_HUMAN = re.compile(r"\b(?:hablar|conversar|contactar)\s+(?:con\s+)?(?:una\s+)?(?:persona|humano|ejecutiv[oa]|asesor)\b", re.I)
_DAY = re.compile(r"\b(?:hoy|mañana|pasado\s+mañana|lunes|martes|miércoles|jueves|viernes|sábado|domingo|desde\s+el\s+\d{1,2}\s+en\s+adelante)\b", re.I)
_CLOCK = re.compile(r"\b(?:a\s+las?\s+\d{1,2}(?::\d{2})?|tipo\s+\d{1,2}(?::\d{2})?|entre\s+\d{1,2}(?::\d{2})?\s+y\s+\d{1,2}(?::\d{2})?|despu[eé]s\s+de\s+las?\s+\d{1,2}(?::\d{2})?|en\s+la\s+(?:mañana|tarde|noche))\b", re.I)
_SCHEDULE_CHANGE = re.compile(r"\b(?:mejor|en\s+vez)\s+(?:el\s+)?(?:lunes|martes|miércoles|jueves|viernes|sábado|domingo)\b", re.I)
_OWNER = re.compile(r"\b(?:quiero\s+(?:arrendar|vender|publicar)\s+mi|tengo\s+una\s+(?:propiedad|casa|departamento|local)|busco\s+(?:una\s+)?corredora|quiero\s+que\s+ustedes\s+la\s+(?:arrienden|vendan)|capt(?:ar|ación)|sin\s+exclusividad|comisi[oó]n)\b", re.I)
_ACK = re.compile(r"^\s*(?:perfecto|ok(?:ay)?|dale|gracias!?|muchas\s+gracias!?|quedo\s+atent[oa]|👍|👌|✅)\s*$", re.I)
_CONTACT_CLOSED = re.compile(r"\b(?:ya\s+me\s+llamaron|ya\s+me\s+contactaron).{0,40}\bgracias\b", re.I)
_NUMERIC_PROPERTY = re.compile(r"\b(?:\d+(?:[.,]\d+)?\s*(?:m2|m²|uf|cuotas?|dormitorios?|baños?)|piso\s*\d+|c[oó]digo\s*\d+|\$\s*\d+)", re.I)
_PROPERTY_SIGNAL = re.compile(r"https?://|www\.|\b(?:c[oó]digo|cod|id|folio)\b", re.I)
_FACT_QUESTION = re.compile(r"precio|cu[aá]nto|gastos?\s+comunes?|dormitorio|bañ|estacion|superficie|metros|orientaci[oó]n|direcci[oó]n|disponib", re.I)
# A bare "cuánto" is a price question only when it is not followed by an
# attribute verb/noun ("cuánto mide", "cuánto son los gastos", etc.).  The
# old broad matcher leaked any numeric property fact into price answers.
_PRICE_QUESTION = re.compile(
    r"\b(?:precio|valor|cu[aá]nto\s+(?:cuesta|sale|vale)|a\s+cu[aá]nto|cu[aá]nto(?!\s+(?:cuestan?|salen|mide|son|tiene|hay|incluye|dormitorios?|bañ(?:o|os)?|metros?|gastos?|cobr\w*|comisi[oó]n|honorarios)\b))\b",
    re.I,
)
_OPERATION_CONFLICT = {"Venta": re.compile(r"\b(?:arriend\w*|arrend\w*|alquiler)\b", re.I), "Arriendo": re.compile(r"\b(?:venta|comprar|compra)\b", re.I)}
_OPERATIONAL_CLAIMS = re.compile(r"\b(?:registr\w*|anot\w*|dej\w*|pas\w*|deriv\w*|comuniqu\w*|gestion\w*).{0,90}\b(?:inter[eé]s|preferencia|visita|ejecutiv|dato|solicitud)|\b(?:te\s+derivo|te\s+dejo\s+el\s+contacto|(?:un\s+)?(?:ejecutiv[oa]|asesor)\s+(?:te\s+)?(?:contactar[aá]|contacte|contacta|llamar[aá]|se\s+contactar[aá]|se\s+contacta))\b", re.I)
_EXECUTIVE_PROMISE_CLAIM = re.compile(
    r"\b(?:el|un|la|tu)\s+ejecutiv[oa]\b(?:\s+[a-záéíóúñü]+){0,20}\s+"
    r"(?:te\s+(?:contactar[aá]|contacta|llamar[aá]|llama|escribir[aá]|escribe)|"
    r"te\s+va\s+a\s+(?:contactar|llamar|escribir)|"
    r"se\s+(?:comunicar[aá]|pondr[aá]\s+en\s+contacto)(?:\s+contigo)?)\b"
    r"|\b(?:te\s+(?:contactar[aá]|contacta|llamar[aá]|llama|escribir[aá]|escribe)|"
    r"te\s+va\s+a\s+(?:contactar|llamar|escribir))\b.{0,40}\b"
    r"(?:el|un|la|tu)\s+ejecutiv[oa]\b",
    re.I,
)
_RAG_NEGATIVE_RESULT = re.compile(
    r"\b(?:no\s+(?:me\s+)?(?:aparece[n]?|encontr[eé]|hay)|ning[uú]n(?:a)?)\b"
    r".{0,70}\b(?:opciones?|alternativas?|resultados?)\b",
    re.I,
)
_RAG_POSITIVE_RESULT = re.compile(
    r"\b(?:encontr[eé]|tenemos|hay|aparecen)\b.{0,60}\b(?:opci[oó]n|alternativa|resultado)\b",
    re.I,
)
_PREMATURE_PERSONAL_DATA = re.compile(
    r"\b(?:confirma|ind[ií]came|d[eé]jame|env[ií]ame|adelant\w*|"
    r"compart\w*|proporcion\w*|entreg\w*).{0,45}\b"
    r"(?:nombre(?:\s+completo)?|rut|correo|email)\b",
    re.I,
)
_UNSUPPORTED_MARKET_CLAIM = re.compile(r"\b(?:valor|precio).{0,55}\b(?:referenc|mercado|rango|zona)|\b(?:rango|mercado)\s+(?:actual|de\s+la\s+zona)\b", re.I)
_UNSUPPORTED_ORG_CLAIM = re.compile(r"\ben\s+procasa\b.{0,100}\b(?:comisi[oó]n|trabajamos|incluye|selecci[oó]n|contrato|publicaci[oó]n)\b|\b(?:un\s+mes\s+de\s+arriendo|m[aá]s\s+iva)\b", re.I)
_PRICE_CLAIM = re.compile(r"\b(?:precio|valor|cuesta|publicad[oa].{0,20}\b(?:uf|\$)|uf\s*[\d.]+)\b", re.I)
_PROPERTY_ATTRIBUTE_CLAIM = re.compile(
    r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\b"
    r"(?:dormitorio|bañ|estacionamiento|bodega|m²|m2|metros|orientaci[oó]n|"
    r"patio|jard[ií]n|quincho|terraza|piscina|logia|balc[oó]n|amoblada?)\b",
    re.I,
)
_PROPERTY_ATTRIBUTE_CLAIMS = (
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\b(?:dormitorio|habitaci[oó]n)\b", re.I), ("dormitorios",)),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\bbañ(?:o|os)\b", re.I), ("banos",)),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\bestacionamiento", re.I), ("estacionamientos",)),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\bbodega\b", re.I), ("bodega",)),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\b(?:m²|m2|metros?)\b", re.I), ("superficie_util", "superficie_total")),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\borientaci[oó]n\b", re.I), ("orientacion",)),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\bpatio\b", re.I), ("patio",)),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\bjard[ií]n\b", re.I), ("jardin",)),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\bquincho\b", re.I), ("quincho",)),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\bterraza\b", re.I), ("terraza",)),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\bpiscina\b", re.I), ("piscina",)),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\blogia\b", re.I), ("logia",)),
    (re.compile(r"\b(?:tiene|cuenta\s+con|incluye|posee|dispone\s+de)\b.{0,55}\bbalc[oó]n\b", re.I), ("balcon",)),
)
_FINANCING_CLAIM = re.compile(r"\b(?:banco|cr[eé]dito).{0,50}\b(?:aprobar[aá]|aprobado|garantizado)\b", re.I)
_UNSUPPORTED_OFFER_CLAIM = re.compile(r"\b(?:propietario|due[nñ]o).{0,50}\b(?:aceptar[aá]|acepta|acept[oó])\b", re.I)
_UNSUPPORTED_OPERATION_PROCESS_CLAIM = re.compile(
    r"\b(?:coordin(?:a|amos|an)|se\s+encarg\w*|te\s+avis\w*|avisamos)\b"
    r".{0,90}\b(?:visitas?|visite|visitar|interesad|agend|disponib|propiedad(?:es)?|tasar)\b"
    r"|\bte\s+avisamos\b"
    r"|\b(?:la\s+agenda|la\s+disponibilidad|la\s+coordinaci[oó]n)\b"
    r".{0,40}\bmanej\w*\b",
    re.I,
)
_UNSUPPORTED_ORG_POLICY_CLAIM = re.compile(
    r"\b(?:trabajamos|cobramos|ofrecemos|incluimos)\b.{0,80}\b"
    r"(?:exclusividad|comisi[oó]n|corretaje|publicaci[oó]n|portales|visita)\b"
    r"|\b(?:sin|con)\s+exclusividad\b"
    r"|\b(?:sin\s+costo|sin\s+compromiso|gratuit\w*)\b"
    r"|\b(?:m[aá]s\s+de\s+\d+\s+a[nñ]os|\d+\s+a[nñ]os\s+de\s+experiencia)\b",
    re.I,
)
_UNSUPPORTED_PROPERTY_NARRATIVE_CLAIM = re.compile(
    r"\b(?:buena\s+luz(?:\s+natural)?|luz\s+natural|luminosidad|buena\s+distribuci[oó]n|"
    r"buena\s+conectividad|sector\s+consolidado|todo\s+a\s+mano|"
    r"cerca\s+de\s+(?:todo|metro|comercio|caf[eé]s|[aá]reas\s+verdes)|"
    r"a\s+pocas\s+cuadras|estaciones?\s+(?:de\s+)?metro|"
    r"aprovecha\s+cada\s+rinc[oó]n|espacio\s+pensado\s+para|"
    r"zona\s+muy\s+consolidada|vida\s+diaria\s+se\s+hace\s+m[aá]s\s+pr[aá]ctica|"
    r"distribuci[oó]n\s+que\s+aprovecha|buena\s+conexi[oó]n\s+a\s+metro|"
    r"bien\s+conectad[oa]|calidad\s+de\s+vida|sin\s+depender\s+del\s+auto|"
    r"traslados\s+largos|excelente\s+ubicaci[oó]n|ubicaci[oó]n\s+privilegiada|"
    r"buena\s+ubicaci[oó]n|espacio\s+que\s+destaca|servicios?\s+cerca|"
    r"[aá]reas\s+comunes|conserjer[ií]a|todo\s+lo\s+que\s+necesitas|"
    r"vida\s+(?:c[oó]moda|tranquila)|entorno\s+ideal)\b",
    re.I,
)
_UNSUPPORTED_EXECUTIVE_ACTION_CLAIM = re.compile(
    r"\b(?:te\s+(?:conecto|pongo\s+en\s+contacto)|"
    r"(?:el|un|la|tu)\s+ejecutiv[oa](?:\s+[a-záéíóúñü]+){0,10}\s+"
    r"(?:te\s+(?:explicar[aá]|orientar[aá]|asesorar[aá])|"
    r"(?:explicar[aá]|orientar[aá]|asesorar[aá])\s+(?:el\s+proceso|las\s+condiciones))|"
    r"puedo\s+(?:pedir|solicitar|conseguir)\w*.{0,80}\b(?:ejecutiv[oa]|fotos?|dato|informaci[oó]n)\b)\b",
    re.I,
)


def _fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).casefold().strip()


def normalize_operation(value: object) -> str | None:
    value = _fold(value)
    if any(term in value for term in ("venta", "comprar", "compra")):
        return "Venta"
    if any(term in value for term in ("arriendo", "arrendar", "alquiler", "alquilar")):
        return "Arriendo"
    return None


def normalize_financing_status(value: object) -> str:
    text = _fold(value)
    if text in {"cash", "contado", "recursos_propios"}: return "cash"
    if text in {"preapproved", "mortgage_preapproved", "credito_aprobado"}: return "mortgage_preapproved"
    if text in {"needs_financing", "mortgage_not_started", "not_started"}: return "mortgage_not_started"
    return "unknown"


def normalize_rental_docs_readiness(value: object) -> str:
    text = _fold(value)
    if text in {"not_ready", "needs_guidance", "unknown"}: return "needs_guidance" if text != "unknown" else "unknown"
    if text in {"ready", "documents_ready"}: return "ready"
    return "unknown"


def update_fallback_streak(state: dict, *, context_key: str, failed: bool) -> dict:
    previous = dict(state or {})
    count = int(previous.get("fallback_consecutive_count", 0) or 0)
    return {**previous, "fallback_context_key": context_key,
            "fallback_consecutive_count": count + 1 if failed else 0}


def extract_readiness(message: str, operation: str | None) -> dict:
    """Extract only explicit customer statements before choosing the NBA."""
    text = _fold(message)
    result: dict[str, str] = {}
    if normalize_operation(operation) == "Venta":
        if re.search(r"\b(?:no|todavia no|aun no)\s+(?:tengo\s+)?(?:credito\s+)?preaprob", text):
            result["financing_status"] = "mortgage_not_started"
        elif re.search(r"\b(?:credito\s+)?preaprob|\bcredito\s+aprobado\b", text):
            result["financing_status"] = "mortgage_preapproved"
        elif (re.search(r"\b(?:al\s+contado|sera\s+al\s+contado|efectivo)\b", text) and not re.search(r"\bsi\s+(?:pago|compr[oa])\s+al\s+contado\b", text)) or re.search(r"\b(?:vendi|vendimos)\s+mi\s+casa\b.{0,80}\b(?:dinero|recursos)\s+disponible", text):
            result["financing_status"] = "cash"
    if normalize_operation(operation) == "Arriendo":
        if re.search(r"\b(?:no\s+se|no\s+tengo|que\s+documentos|que\s+requisitos)\b", text):
            result["rental_docs_readiness"] = "needs_guidance"
        elif re.search(r"\b(?:ya\s+)?tengo\b.{0,45}\b(?:documentos|antecedentes|papeles)\b", text):
            result["rental_docs_readiness"] = "ready"
    return result


def extract_visit_preference(message: str, *, visit_context: bool = False) -> str | None:
    text = str(message or "").strip()
    if not text or _OWNER.search(text) or _NUMERIC_PROPERTY.search(text):
        return None
    # A correction such as "el sábado no puedo, mejor domingo" replaces the
    # prior preference. Restrict extraction to the correction clause so the
    # old day cannot leak into the new visit state.
    schedule_change = _SCHEDULE_CHANGE.search(text)
    extraction_text = schedule_change.group(0) if schedule_change else text
    day = _DAY.findall(extraction_text)
    clock = _CLOCK.findall(extraction_text)
    day_with_hour = bool(day and re.search(r"\b(?:lunes|martes|miércoles|jueves|viernes|sábado|domingo)\s+(?:a\s+las?\s+)?\d{1,2}(?::\d{2})?", text, re.I))
    if not (_VISIT.search(text) or visit_context or _SCHEDULE_CHANGE.search(text) or clock or day_with_hour):
        return None
    values = []
    for match in list(_DAY.finditer(extraction_text)) + list(_CLOCK.finditer(extraction_text)):
        value = match.group(0).strip()
        if value not in values:
            values.append(value)
    # Bare hours are valid only with a day/date or an explicit visit context.
    # Exclude numbers tied to property facts (m², floor, bedrooms,
    # installments, UF and price).
    if not schedule_change and day and (visit_context or _VISIT.search(text)):
        for match in re.finditer(r"(?<![\w:])(?:[01]?\d|2[0-3])(?::[0-5]\d)?(?!\s*(?:m2|m²|uf|cuotas?|dormitorios?|baños?|piso))\b", text, re.I):
            token = match.group(0)
            before = text[max(0, match.start() - 12):match.start()]
            after = text[match.end():match.end() + 12]
            if re.search(r"(?:\$|uf|m2|m²|piso|dormitorio|baño|cuota)\s*$", before, re.I):
                continue
            if re.match(r"\s*(?:m2|m²|uf|cuotas?|dormitorios?|baños?|piso)\b", after, re.I):
                continue
            if token not in values:
                values.append(token)
    return "; ".join(values) or None


def _last_user(messages: list[dict]) -> dict:
    return next((item for item in reversed(messages or []) if item.get("role") == "user"), {})


def build_conversation_state(lead: dict, messages: list[dict] | None = None, events=None, property_context=None) -> dict:
    messages = [item for item in (messages or lead.get("messages") or []) if item.get("role") in {"user", "assistant"}]
    prospect = lead.get("prospecto") or {}
    context = property_context or {}
    last = _last_user(messages)
    message = str(last.get("content") or "")
    operation = normalize_operation(context.get("operation") or prospect.get("operacion") or lead.get("operacion"))
    persisted_search_state = prospect.get("rag_search_state") or lead.get("rag_search_state") or {}
    persisted_search_criteria = (
        persisted_search_state.get("criteria")
        if isinstance(persisted_search_state, dict)
        else {}
    ) or prospect.get("search_criteria") or lead.get("search_criteria") or {}
    search_criteria = dict(persisted_search_criteria) if isinstance(persisted_search_criteria, dict) else {}
    search_intent_seen = bool(
        prospect.get("search_intent") or lead.get("search_intent")
        or (persisted_search_state.get("search_intent") if isinstance(persisted_search_state, dict) else False)
        or search_criteria
    )
    # Rebuild the search state from causal customer turns.  This keeps
    # criterion-only follow-ups ("Arrendar", "Providencia", "2 dormitorios")
    # attached to the original search without treating assistant text or
    # export artefacts as new customer criteria.
    for item in messages:
        if item.get("role") != "user":
            continue
        content = str(item.get("content") or "")
        if is_property_search_intent(content):
            search_intent_seen = True
        if search_intent_seen:
            search_criteria = extract_search_criteria(content, search_criteria)
    current_property_specific = contains_property_identifier(message)
    if current_property_specific:
        # A current explicit URL/code belongs to the property-specific route;
        # previous search criteria remain useful history but are not the
        # active intent for this turn.
        current_search_intent = False
    else:
        current_search_intent = bool(search_intent_seen)
        last_search_index = max(
            (index for index, item in enumerate(messages)
             if item.get("role") == "user" and is_property_search_intent(str(item.get("content") or ""))),
            default=-1,
        )
        last_identifier_index = max(
            (index for index, item in enumerate(messages)
             if item.get("role") == "user" and contains_property_identifier(str(item.get("content") or ""))),
            default=-1,
        )
        if last_identifier_index > last_search_index:
            # A resolved explicit listing closes the previous general-search
            # turn.  A later explicit search will reopen it.
            current_search_intent = False
    if not operation:
        operation = normalize_operation(search_criteria.get("operation") or search_criteria.get("operacion"))
    owner = "human" if lead.get("human_active") or lead.get("conversation_owner") == "human" else "bot"
    owner_seen_in_history = any(
        item.get("role") == "user" and _OWNER.search(str(item.get("content") or ""))
        for item in messages
    )
    persisted_actor = str(
        lead.get("actor_intent") or prospect.get("actor_intent") or ""
    ).upper()
    # Captation is a conversation role, not a one-turn keyword.  Keep it
    # active for follow-up questions such as "¿cómo hacen las visitas?" so
    # operational language about the service is not reclassified as a buyer
    # visit request.
    actor_intent = "OWNER" if (_OWNER.search(message) or owner_seen_in_history or persisted_actor == "OWNER") else "CUSTOMER"
    prior_visit = actor_intent == "CUSTOMER" and any(_VISIT.search(str(item.get("content") or "")) for item in messages[:-1])
    preference = extract_visit_preference(message, visit_context=prior_visit)
    readiness = extract_readiness(message, operation)
    asked_fields = []
    if any(re.search(r"cr[eé]dito hipotecario|recursos propios", str(item.get("content") or ""), re.I) for item in messages if item.get("role") == "assistant"):
        asked_fields.append("financing_status")
    event_types = {item.get("event_type") for item in (events or [])}
    # An explicit visit request and its first preference can arrive in the
    # same customer turn. Keep the public status at interest-detected until
    # the preference is carried by a subsequent scheduling turn; the value is
    # still persisted in ``visit_preference`` and therefore is not lost.
    visit_status = "SCHEDULED" if "visit_scheduled" in event_types else (
        "PREFERENCE_CAPTURED" if preference and not _VISIT.search(message) else
        ("INTEREST_DETECTED" if _VISIT.search(message) else "NONE")
    )
    return {
        "conversation_id": lead.get("conversation_id") or str(lead.get("_id") or ""),
        "lead_id": str(lead.get("_id") or lead.get("lead_id") or ""),
        "property_id": context.get("property_id") or context.get("property_code") or prospect.get("codigo"),
        "property_code": context.get("property_code") or context.get("property_id") or prospect.get("codigo"),
        "operation": operation,
        "search_intent": current_search_intent,
        "search_criteria": search_criteria,
        "property_specific_intent": current_property_specific,
        "actor_intent": actor_intent,
        "current_intent": "OWNER_SERVICE_REQUEST" if actor_intent == "OWNER" else (
            "PROPERTY_SPECIFIC" if current_property_specific else (
                "PROPERTY_SEARCH" if current_search_intent else (
                    "VISIT_INTENT" if (_VISIT.search(message) or preference) else (
                        "HUMAN_REQUEST" if _HUMAN.search(message) else "EXPLORATORY"
                    )
                )
            )
        ),
        "intent_confidence": 0.95 if (current_property_specific or current_search_intent or _VISIT.search(message) or preference or _HUMAN.search(message)) else 0.55,
        "intent_evidence": "explicit_property_reference" if current_property_specific else (
            "property_search_context" if current_search_intent else (
                "explicit_visit_or_preference" if (_VISIT.search(message) or preference) else "customer_message"
            )
        ),
        "customer_goal": message or None,
        "financing_status": readiness.get("financing_status") or prospect.get("financing_status") or "unknown",
        "rental_docs_readiness": readiness.get("rental_docs_readiness") or prospect.get("rental_docs_readiness") or "unknown",
        "readiness_extracted": readiness,
        "visit_intent": bool(actor_intent == "CUSTOMER" and (_VISIT.search(message) or preference)),
        "visit_context": prior_visit,
        "visit_status": visit_status,
        "visit_preference": preference or prospect.get("visit_preference"),
        "known_fields": [key for key, value in prospect.items() if value not in (None, "", "unknown")],
        "asked_fields": asked_fields, "last_customer_question": message or None,
        "last_customer_message_id": last.get("message_id"), "last_bot_action": None,
        "next_best_action": None, "conversation_owner": owner, "human_active": owner == "human",
        "frustration_signal": bool(re.search(r"tres\s+veces|no\s+responden|frustr", message, re.I)),
        "updated_at": datetime.now(timezone.utc).isoformat(), "policy_version": POLICY_VERSION, "prompt_version": PROMPT_VERSION,
    }


def _has_fact_for_question(message: str, facts: dict) -> bool:
    text = _fold(message)
    if _PRICE_QUESTION.search(text) and any(facts.get(key) not in (None, "") for key in ("precio_uf", "precio_clp")):
        return True
    mapping = {"gastos": ("gastos_comunes",), "dorm": ("dormitorios",), "bano": ("banos",), "estacion": ("estacionamientos",), "superficie": ("superficie_util", "superficie_total"), "orientacion": ("orientacion",)}
    return any(term in text and any(facts.get(key) not in (None, "") for key in keys) for term, keys in mapping.items())


def build_response_plan(state: dict, user_message: str, facts: dict | None = None) -> dict:
    """Extract customer asks before choosing a commercial action.

    The writer may phrase the result, but cannot discard a factual question
    merely because the same message also signals a visit or handoff.
    """
    text = _fold(user_message)
    topics = []
    for topic, terms in {
        "availability": ("disponib", "vigente"), "common_expenses": ("gastos comunes",),
        "price": ("precio", "valor"), "bedrooms": ("dormitorio", "habitacion"),
        "parking": ("estacion", "parking"),
        "storage": ("bodega",), "surface": ("superficie", "metros", "m2", "mide"),
        "photos": ("fotos", "fotograf"), "negotiation": ("negoci", "oferta", "rebaja"),
        "commission": ("comision", "honorarios", "cobran"),
    }.items():
        if any(term in text for term in terms): topics.append(topic)
    if _PRICE_QUESTION.search(text) and "price" not in topics:
        topics.append("price")
    current_search_signal = bool(
        is_property_search_intent(user_message)
        or extract_search_criteria(user_message)
    ) and not contains_property_identifier(user_message)
    search_active = bool(
        current_search_signal
        and (state.get("search_intent") or is_property_search_intent(user_message))
        and not state.get("property_specific_intent")
    )
    if search_active:
        topics.append("PROPERTY_SEARCH")
    secondary = []
    if _VISIT.search(user_message) or state.get("visit_intent"): secondary.append("VISIT")
    if _HUMAN.search(user_message): secondary.append("HUMAN_HANDOFF")
    return {"actor": state.get("actor_intent"), "primary_intents": list(dict.fromkeys(topics)),
            "secondary_intents": secondary, "facts_required": topics,
            "search_criteria": dict(state.get("search_criteria") or {}),
            "business_actions": (["SEARCH_PROPERTY"] if search_active else []) +
                                (["CAPTURE_VISIT"] if "VISIT" in secondary else []) +
                                (["HANDOFF"] if "HUMAN_HANDOFF" in secondary else [])}


def select_next_best_action(state: dict, user_message: str, *, facts=None, property_resolved=True, information_gap=False, repeated_fallbacks=0) -> dict:
    text = str(user_message or "")
    facts = facts or {}
    if state.get("human_active"):
        action, reason = "HANDOFF_HUMAN", "human_ownership_active"
    elif _ACK.match(text) or _CONTACT_CLOSED.search(text):
        action, reason = "WAIT_CUSTOMER", "acknowledgement_or_contact_closed"
    elif state.get("actor_intent") == "OWNER":
        action, reason = "ANSWER_ONLY", "owner_service_request_not_visit"
    elif state.get("visit_status") == "SCHEDULED":
        action, reason = "WAIT_CUSTOMER", "visit_already_scheduled"
    elif _HUMAN.search(text):
        action, reason = "HANDOFF_HUMAN", "customer_requested_human_terminal"
    elif state.get("visit_intent") or extract_visit_preference(text, visit_context=bool(state.get("visit_context"))) or state.get("financing_status") == "mortgage_preapproved":
        action, reason = "PROPOSE_VISIT", "explicit_visit_signal_precedes_qualification"
    elif state.get("operation") in _OPERATION_CONFLICT and _OPERATION_CONFLICT[state["operation"]].search(text):
        action, reason = "ASK_CLARIFICATION", "operation_context_conflict"
    elif (
        (is_property_search_intent(text) or extract_search_criteria(text))
        and (state.get("search_intent") or is_property_search_intent(text))
        and not state.get("property_specific_intent")
        and not contains_property_identifier(text)
    ):
        action, reason = "SEARCH_PROPERTY", "progressive_property_search"
    elif (
        not property_resolved
        and (_PROPERTY_SIGNAL.search(text) or _FACT_QUESTION.search(text))
        and not _has_fact_for_question(text, facts)
    ):
        action, reason = "ASK_CLARIFICATION", "property_context_unresolved"
    elif state.get("operation") == "Arriendo" and state.get("rental_docs_readiness") == "needs_guidance":
        action, reason = "ASK_QUALIFICATION", "provide_rental_guidance_without_sensitive_request"
    elif state.get("operation") == "Arriendo" and re.search(r"requisitos?|document", text, re.I):
        action, reason = "ASK_QUALIFICATION", "rental_requirements_progressive"
    elif state.get("operation") == "Venta" and re.search(r"me interesa", text, re.I) and state.get("financing_status") == "unknown":
        action, reason = "ASK_QUALIFICATION", "progressive_financing_after_interest"
    elif _has_fact_for_question(text, facts) or "?" in text:
        action, reason = "ANSWER_ONLY", "answer_question_before_qualification"
    else:
        action, reason = "ANSWER_ONLY", "default_conversational_response"
    return {"next_best_action": action, "reason": reason, "confidence": 0.95 if action != "ANSWER_ONLY" else 0.8, "policy_version": POLICY_VERSION}


def _fact_answer(message: str, facts: dict) -> str | None:
    text = _fold(message)
    if _PRICE_QUESTION.search(text):
        if facts.get("precio_uf") not in (None, ""):
            return f"El precio informado es UF {facts['precio_uf']}."
        if facts.get("precio_clp") not in (None, ""):
            return f"El precio informado es ${facts['precio_clp']}."
    choices = (("gastos", "gastos_comunes", "Los gastos comunes informados son {value}."), ("estacion", "estacionamientos", "La propiedad informa {value} estacionamiento(s)."), ("dorm", "dormitorios", "La propiedad informa {value} dormitorio(s)."), ("bano", "banos", "La propiedad informa {value} baño(s)."))
    for term, key, template in choices:
        if term in text and facts.get(key) not in (None, ""):
            return template.format(value=facts[key])
    if "orientacion" in text and not facts.get("orientacion"):
        return "No tengo la orientación confirmada en la información disponible."
    return None


def deterministic_response(state: dict, action: dict, facts: dict, user_message: str) -> str | None:
    kind = action.get("next_best_action")
    if state.get("human_active"):
        return ""
    if kind == "WAIT_CUSTOMER":
        return ""
    if state.get("actor_intent") == "OWNER":
        return "Podemos revisar los antecedentes de tu propiedad y el servicio de captación. Un ejecutivo puede confirmar las condiciones aplicables."
    if kind == "HANDOFF_HUMAN":
        return "Entiendo. Voy a derivar tu solicitud a un ejecutivo para que continúe la atención."
    if kind == "SEARCH_PROPERTY":
        criteria = dict(state.get("search_criteria") or {})
        if isinstance(facts.get("search_criteria"), dict):
            criteria.update(facts["search_criteria"])
        return build_property_search_response(
            criteria,
            rag_result_count=facts.get("rag_result_count"),
        )
    if kind == "PROPOSE_VISIT":
        factual = _fact_answer(user_message, facts)
        preference = state.get("visit_preference")
        if re.search(r"\b(?:fotos?|fotograf[ií]as)\b", user_message, re.I):
            prefix = "No puedo confirmar fotos específicas con la información disponible. "
        else:
            prefix = ""
        visit = (f"Gracias. Consideraremos tu preferencia de {preference} para revisar la coordinación de visita con el ejecutivo. La disponibilidad debe confirmarse antes de agendar."
                 if preference else "Podemos revisar la coordinación de una visita con el ejecutivo. ¿Qué día u horario te acomoda?")
        return f"{prefix}{factual} {visit}".strip() if factual else prefix + visit
    if kind == "ASK_CLARIFICATION":
        if action.get("reason") == "operation_context_conflict":
            return "La información disponible corresponde a la operación publicada. Para confirmar si existe la alternativa que consultas, un ejecutivo debe revisarla."
        return "Para responderte con precisión, ¿me puedes enviar el enlace de la publicación de la propiedad?"
    if kind == "ASK_QUALIFICATION" and state.get("rental_docs_readiness") == "needs_guidance":
        return "Para arriendo suelen solicitar antecedentes de renta e identidad; un ejecutivo puede indicarte los requisitos vigentes para esta propiedad."
    if kind == "ANSWER_ONLY":
        if re.search(r"\b(?:fotos?|fotograf[ií]as)\b", user_message, re.I) and state.get("visit_intent"):
            return "No puedo confirmar fotos específicas con la información disponible. Podemos revisar la coordinación de visita con el ejecutivo."
        if re.search(r"\b(?:hipotecari|cr[eé]dito).{0,50}\b(?:apoyan|gesti[oó]n|ayuda)\b", user_message, re.I):
            return "Puedo orientarte sobre el proceso, pero no puedo confirmar una gestión de crédito específica desde aquí. Un ejecutivo puede aclarar el alcance del apoyo disponible."
        if re.search(r"\bres[eé]rv", user_message, re.I):
            return "No puedo confirmar esa solicitud sin verificar disponibilidad. Para avanzar, podemos revisar la coordinación de una visita con el ejecutivo."
        answer = _fact_answer(user_message, facts)
        if answer:
            return answer
        if state.get("rental_docs_readiness") == "needs_guidance":
            return "Para arriendo suelen solicitar antecedentes de renta e identidad; un ejecutivo puede indicarte los requisitos vigentes para esta propiedad."
        return "No tengo ese antecedente confirmado en la información disponible. Si me compartes el enlace de la publicación, puedo orientar la consulta con precisión."
    return "No tengo ese antecedente confirmado en la información disponible. Si me compartes el enlace de la publicación, puedo orientar la consulta con precisión."


def build_policy_instruction(state: dict, action: dict, facts: dict) -> str:
    facts_text = "\n".join(f"- {key}: {value}" for key, value in (facts or {}).items() if value not in (None, "")) or "- Sin hechos confirmados."
    terminal = "No agregues preguntas, CTA ni calificación." if action["next_best_action"] == "HANDOFF_HUMAN" else ""
    return f"""[PHASE 3 {POLICY_VERSION}]
Acción obligatoria: {action['next_best_action']}; razón: {action['reason']}.
Primero responde hechos disponibles. Una intención explícita de visita prevalece sobre calificación.
No inventes disponibilidad, reserva, registro, notificación, contacto futuro o acciones ya realizadas.
No digas que registraste/registrarás interés, que gestionas/gestionarás, ni que un ejecutivo contactará al cliente, salvo evidencia explícita del sistema (que aquí no existe).
No pidas nombre, RUT, correo ni teléfono en este turno.
No confirmes visitas. {terminal}
Hechos autorizados:\n{facts_text}
Devuelve una respuesta breve, natural y un JSON válido."""


def extract_claims(response: str) -> list[dict]:
    text = str(response or "")
    claims = []
    operational_match = (
        _OPERATIONAL_CLAIMS.search(text)
        or _EXECUTIVE_PROMISE_CLAIM.search(text)
        or _UNSUPPORTED_OPERATION_PROCESS_CLAIM.search(text)
        or _UNSUPPORTED_EXECUTIVE_ACTION_CLAIM.search(text)
    )
    if operational_match:
        claims.append({"claim_type": "unsupported_operational_claim", "text": operational_match.group(0)})
    if re.search(r"\b(?:visita|visitar|reserva\w*|agenda\w*)\b.{0,35}\b(?:confirmada|agendada|reservada)\b", text, re.I):
        claims.append({"claim_type": "unsupported_reservation", "text": text})
    if re.search(r"\b(?:hay|tenemos)\s+disponibilidad\b", text, re.I):
        claims.append({"claim_type": "availability_claim", "text": text})
    return claims


def _has_verified_property_attribute(facts: dict, keys: tuple[str, ...]) -> bool:
    return any(facts.get(key) not in (None, "") for key in keys)


def _unsupported_property_attribute_claim(response: str, facts: dict) -> bool:
    """Detect a concrete physical attribute claim without its matching fact.

    Checking whether *any* property fact exists is insufficient: a record with
    price and bedrooms must not authorize a model-generated claim about a
    patio, terrace, orientation, or storage room.
    """
    # An explicit lack-of-data sentence is the safe answer for an unknown
    # attribute.  It must not be mistaken for a positive claim merely because
    # it repeats the attribute name (for example, "no puedo confirmar si
    # incluye bodega").  Evaluate sentence-by-sentence so a later positive
    # claim in the same response is still checked normally.
    sentences = re.split(r"(?<=[.!?])\s+", str(response or ""))
    unknown_prefix = re.compile(
        r"\b(?:no\s+puedo\s+confirmar|no\s+tengo|no\s+aparece|no\s+cuento)\b"
        r"[^.!?;]{0,24}$|\bsin\s+(?:la\s+)?informaci[oó]n\b[^.!?;]{0,24}$|"
        r"\bno\s+se\s+encuentra\b[^.!?;]{0,24}$",
        re.I,
    )
    for pattern, keys in _PROPERTY_ATTRIBUTE_CLAIMS:
        if _has_verified_property_attribute(facts, keys):
            continue
        for sentence in sentences:
            for match in pattern.finditer(sentence):
                # The unknown qualifier must be immediately attached to the
                # same attribute. This still catches "No tengo fotos del
                # patio, pero la casa tiene un patio..." as a positive patio
                # claim while allowing "No puedo confirmar si incluye bodega".
                prefix = sentence[:match.start()]
                if not unknown_prefix.search(prefix):
                    return True
    return False


def validate_response(response: str, *, state: dict, facts=None, fact_sources=None, property_code=None, availability_confirmed=False, crm_handoff_confirmed=False, previous_responses=None) -> dict:
    reasons, claims = [], []
    facts = facts or {}
    for claim in extract_claims(response):
        recorded_preference = bool(
            state.get("visit_preference")
            and re.search(r"\b(?:registr\w*|anot\w*|dej\w*)\b.{0,60}\bpreferencia\b", response, re.I)
        )
        if claim["claim_type"] == "unsupported_operational_claim" and (
            re.search(r"\b(?:voy a|puedo)\s+(?:derivar|revisar)\b", response, re.I)
            or recorded_preference
        ):
            status = "SUPPORTED"
        else:
            status = "UNSUPPORTED"
            reasons.append(claim["claim_type"])
        claims.append({**claim, "status": status, "required_sources": [], "evidence_ids": []})
    if state.get("human_active") and response:
        reasons.append("human_ownership_active")
    if _PREMATURE_PERSONAL_DATA.search(response):
        reasons.append("premature_personal_data_request")
    if _UNSUPPORTED_MARKET_CLAIM.search(response):
        reasons.append("unsupported_market_claim")
    if _UNSUPPORTED_ORG_CLAIM.search(response) or _UNSUPPORTED_ORG_POLICY_CLAIM.search(response):
        reasons.append("unsupported_organization_claim")
    if _PRICE_CLAIM.search(response) and not any(facts.get(key) not in (None, "") for key in ("precio_uf", "precio_clp")):
        reasons.append("unsupported_price_claim")
    if _PROPERTY_ATTRIBUTE_CLAIM.search(response) and _unsupported_property_attribute_claim(response, facts):
        reasons.append("unsupported_property_fact_claim")
    if _UNSUPPORTED_PROPERTY_NARRATIVE_CLAIM.search(response):
        reasons.append("unsupported_property_fact_claim")
    if _FINANCING_CLAIM.search(response):
        reasons.append("unsupported_financing_claim")
    if _UNSUPPORTED_OFFER_CLAIM.search(response):
        reasons.append("unsupported_offer_claim")
    rag_result_count = facts.get("rag_result_count")
    if rag_result_count is not None:
        try:
            rag_result_count = int(rag_result_count)
        except (TypeError, ValueError):
            rag_result_count = None
    if rag_result_count is not None:
        if rag_result_count > 0 and _RAG_NEGATIVE_RESULT.search(response):
            reasons.append("rag_result_mismatch")
        elif rag_result_count == 0 and _RAG_POSITIVE_RESULT.search(response):
            reasons.append("rag_result_mismatch")
        elif rag_result_count > 0 and not _RAG_POSITIVE_RESULT.search(response):
            reasons.append("rag_result_omitted")
    folded_response = _fold(response)
    if re.search(r"\b(?:tenemos|hay|alta|esta|si)\b.{0,25}\bdisponib\w*\b", folded_response) and not re.search(r"\b(?:no tengo|sin)\b.{0,30}\bdisponib\w*\b", folded_response):
        reasons.append("availability_claim")
        claims.append({"claim_type":"availability_claim","text":response,"status":"UNSUPPORTED","required_sources":[],"evidence_ids":[]})
    if re.search(r"\b(?:confirma|confirmar[aá]|valida|validar[aá])\s+la\s+disponibilidad\b", folded_response) and not re.search(r"\b(?:debe|deben)\s+(?:confirmar|validar)\b", folded_response):
        reasons.append("availability_claim")
        claims.append({"claim_type":"availability_claim","text":response,"status":"UNSUPPORTED","required_sources":[],"evidence_ids":[]})
    if re.search(r"gastos comunes|estacionamiento|direcci[oó]n|reserv", response, re.I) and not facts:
        reasons.append("unsupported_property_claim")
        claims.append({"claim_type":"property_claim","text":response,"status":"UNSUPPORTED","required_sources":[],"evidence_ids":[]})
    if re.search(r"ejecutiv[oa].*(?:notificado|llamar)", response, re.I):
        reasons.append("unsupported_operational_claim")
        claims.append({"claim_type":"operational_claim","text":response,"status":"UNSUPPORTED","required_sources":[],"evidence_ids":[]})
    if state.get("financing_status") != "unknown" and re.search(r"cr[eé]dito hipotecario|recursos propios", response, re.I):
        reasons.append("question_repeats_known_field")
    if property_code and re.search(r"\bP-\d+\b", response, re.I):
        for code in re.findall(r"\bP-\d+\b", response, re.I):
            if code.casefold() != str(property_code).casefold(): reasons.append("property_context_mismatch")
    if re.search(r"gastos comunes", response, re.I) and facts.get("gastos_comunes") not in (None, ""):
        source = facts["gastos_comunes"] if isinstance(facts["gastos_comunes"], dict) else {}
        claims.append({"claim_type":"property_fact","text":response,"status":"SUPPORTED","required_sources":[source.get("source")] if source.get("source") else [],"evidence_ids":[source.get("evidence_id")] if source.get("evidence_id") else []})
    if state.get("operation") == "Venta" and re.search(r"\b(?:esta|está|publicada|disponible)\s+(?:en\s+)?arriendo\b", response, re.I):
        reasons.append("operation_mismatch")
    if state.get("operation") == "Arriendo" and re.search(r"\b(?:esta|está|publicada|disponible)\s+(?:en\s+)?venta\b", response, re.I):
        reasons.append("operation_mismatch")
    return {"valid": not reasons, "reasons": list(dict.fromkeys(reasons)), "claims": claims, "policy_version": POLICY_VERSION}


def repair_response(response: str, validation: dict, *, state: dict, **kwargs) -> str:
    if state.get("human_active"):
        return ""
    unsafe = {"unsupported_operational_claim", "unsupported_reservation", "availability_claim", "premature_personal_data_request", "unsupported_market_claim", "unsupported_organization_claim", "unsupported_price_claim", "unsupported_property_fact_claim", "unsupported_financing_claim", "unsupported_offer_claim", "rag_result_mismatch", "rag_result_omitted"}
    if {"rag_result_mismatch", "rag_result_omitted"}.intersection(validation.get("reasons") or []):
        try:
            result_count = int((kwargs.get("facts") or {}).get("rag_result_count"))
        except (TypeError, ValueError):
            result_count = 0
        if result_count > 0:
            return "Encontré una alternativa que coincide con los criterios que compartiste. Puedo mostrarte sus detalles para que la revises."
        return "No encontré opciones exactas con esos criterios. Podemos ampliar la búsqueda si quieres."
    if not unsafe.intersection(validation.get("reasons") or []):
        return str(response or "").strip()
    sentences = re.split(r"(?<=[.!?])\s+", str(response or "").strip())
    facts = kwargs.get("facts") or {}
    has_price = any(facts.get(key) not in (None, "") for key in ("precio_uf", "precio_clp"))
    kept = [sentence for sentence in sentences if not _OPERATIONAL_CLAIMS.search(sentence) and not _EXECUTIVE_PROMISE_CLAIM.search(sentence) and not _UNSUPPORTED_OPERATION_PROCESS_CLAIM.search(sentence) and not _UNSUPPORTED_EXECUTIVE_ACTION_CLAIM.search(sentence) and not _PREMATURE_PERSONAL_DATA.search(sentence) and not _UNSUPPORTED_MARKET_CLAIM.search(sentence) and not _UNSUPPORTED_ORG_CLAIM.search(sentence) and not _UNSUPPORTED_ORG_POLICY_CLAIM.search(sentence) and not (_PRICE_CLAIM.search(sentence) and not has_price) and not (_PROPERTY_ATTRIBUTE_CLAIM.search(sentence) and _unsupported_property_attribute_claim(sentence, facts)) and not _UNSUPPORTED_PROPERTY_NARRATIVE_CLAIM.search(sentence) and not _FINANCING_CLAIM.search(sentence) and not _UNSUPPORTED_OFFER_CLAIM.search(sentence) and not re.search(r"\b(?:confirmada|agendada|reservada|hay disponibilidad|alta disponibilidad|esta disponible|está disponible|gestione|lo haga|te parece bien|te gustar[ií]a que)\b", sentence, re.I)]
    repaired = " ".join(kept).strip()
    if (
        state.get("actor_intent") == "OWNER"
        and "unsupported_operational_claim" in (validation.get("reasons") or [])
        and re.search(
            r"\b(?:visita|visitas|coordina|coordinaci[oó]n)\b",
            str(kwargs.get("customer_message") or ""),
            re.I,
        )
    ):
        return "No tengo confirmado el procedimiento específico para las visitas de tu propiedad. Un ejecutivo puede explicarte las condiciones aplicables."
    if state.get("visit_intent"):
        safe_visit = "La disponibilidad debe confirmarse antes de agendar. ¿Qué día u horario te acomoda?"
        # A legacy transform may already have left the no-confirmation
        # sentence without the follow-up question. Do not append that same
        # sentence a second time while repairing the model output.
        safe_visit_prefix = safe_visit.split("¿", 1)[0].strip()
        if safe_visit_prefix.casefold() in repaired.casefold():
            return repaired
        if re.search(r"\b(?:qu[eé]|que)\s+d[ií]a\b|\b(?:horario|rango horario)\b", repaired, re.I):
            if re.search(r"\bdisponibilidad\b", repaired, re.I):
                return repaired
            return f"{repaired} La disponibilidad debe confirmarse antes de agendar.".strip()
        return f"{repaired} {safe_visit}".strip()
    if state.get("actor_intent") == "OWNER":
        return repaired or "No tengo las condiciones comerciales confirmadas en la información disponible."
    return repaired or "La disponibilidad y coordinación deben confirmarse antes de agendar."


def build_executive_summary(lead: dict, messages=None, state=None, events=None) -> dict:
    state = state or build_conversation_state(lead, messages, events)
    evidence = [item.get("message_id") for item in (messages or []) if item.get("message_id")] + [item.get("event_id") for item in (events or []) if item.get("event_id")]
    return {"conversation_id":state.get("conversation_id"),"property_code": state.get("property_code"), "operation": state.get("operation"), "visit_status": state.get("visit_status"), "visit_preference": state.get("visit_preference"), "financing_status":state.get("financing_status"), "rental_docs_readiness":state.get("rental_docs_readiness"), "next_best_action":state.get("next_best_action"), "evidence_ids":evidence, "readiness": state.get("financing_status") if state.get("operation") == "Venta" else state.get("rental_docs_readiness")}
