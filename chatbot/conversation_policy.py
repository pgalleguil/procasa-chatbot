"""Deterministic safeguards for outbound chatbot conversation behaviour."""
from __future__ import annotations

import re
import unicodedata


_PHONE_REQUEST = re.compile(
    r"(?:ind[ií]came|necesito|d[eé]jame|comparte|dame|p[aá]same|"
    r"(?:me\s+)?(?:puedes?|das?)\s+(?:(?:compartir|dar|pasar)\s+)?|cu[aá]l\s+es)"
    r".{0,80}(?:tu\s+)?(?:tel[eé]fono|n[uú]mero(?:\s+celular)?|celular|whatsapp|"
    r"n[uú]mero\s+de\s+contacto)",
    re.IGNORECASE | re.DOTALL,
)

_VISIT_INTENT_PATTERNS = (
    r"\b(?:quiero|quisiera|me\s+gustar[ií]a|me\s+encantar[ií]a)\s+(?:ver(?:la|lo|la\s+propiedad|el\s+inmueble)?|visitar(?:la|lo)?|conocer(?:la|lo)?)\b",
    r"\b(?:se\s+puede|es\s+posible)\s+(?:visitar|ver(?:la|lo)?)\b",
    r"\b(?:cu[aá]ndo|qu[eé]\s+d[ií]a|a\s+qu[eé]\s+hora)\s+(?:la\s+puedo\s+ver|puedo\s+ir|se\s+puede\s+visitar|podemos\s+ir)\b",
    r"\b(?:puedo|podr[ií]a|me\s+acomoda)\s+ir\s+(?:a\s+)?(?:verla|verlo|conocerla|conocerlo)\b",
    r"\b(?:puedo|podr[ií]a)\s+ir\s+(?:ma[nñ]ana|hoy|el\s+(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo))\b",
    r"\b(?:tienen|hay)\s+(?:hora|horario|disponibilidad)\s+para\s+(?:verla|verlo|visitarla|visitarlo)\b",
    r"\b(?:tienen|hay)\s+disponibilidad\s+(?:para\s+)?(?:visita|ir|verla|verlo)\b",
    r"\b(?:agendemos|coordinemos)\b(?:.{0,40}\bvisita\b)?",
    r"\b(?:coordinar|agendar)\s+(?:una\s+)?visita\b",
    r"\b(?:quiero|me\s+gustar[ií]a)\s+conocer\s+(?:la|el)\b",
)
_VISIT_INTENT_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _VISIT_INTENT_PATTERNS)

_VISIT_ACCEPTANCE_RE = re.compile(
    r"^(?:s[ií]|claro|dale|por\s+supuesto|perfecto|ok|okay|ya|obvio|"
    r"me\s+encanta(?:r[ií]a)?|adelante|puedes|bueno|de\s+acuerdo)(?:[,.!\s].*)?$",
    re.IGNORECASE,
)
_VISIT_DECLINE_RE = re.compile(
    r"^(?:no|no\s+gracias|prefiero\s+(?:d[aá]rselos|coordinar|hablar)|"
    r"no\s+quiero(?:\s+dar)?|despu[eé]s|m[aá]s\s+adelante|"
    r"prefiero\s+dar(?:los|le)|mejor\s+con\s+el\s+ejecutivo)(?:[,.!\s].*)?$",
    re.IGNORECASE,
)

_ALTERNATIVE_REQUEST_RE = re.compile(
    r"\b(?:algo\s+parecido|otras?|otra\s+propiedad|qu[eé]\s+m[aá]s\s+tienen|"
    r"mu[eé]strame\s+otras|mu[eé]strame\s+m[aá]s|busco\s+otra|tienen\s+algo\s+m[aá]s|"
    r"otra\s+comuna|cambiar\s+de\s+comuna)\b",
    re.IGNORECASE,
)
_PROPERTY_REJECTION_RE = re.compile(
    r"\b(?:no\s+me\s+gust(?:a|o|ó)|no\s+me\s+sirve|no\s+me\s+acomoda|"
    r"est[aá]\s+muy\s+(?:cara|caro|pequeñ[ao]|grande)|es\s+muy\s+pequeñ[ao]|"
    r"esa\s+comuna\s+no|no\s+me\s+interesa)\b",
    re.IGNORECASE,
)

_UNCONFIRMED_VISIT_RE = re.compile(
    r"(?:\b(?:tu\s+)?visita\s+(?:qued[oó]|est[aá])\s+(?:agendada|confirmada|reservada)\b|"
    r"\bvisita\s+(?:agendada|confirmada|reservada|est[aá]\s+confirmada)\b|"
    r"\b(?:te\s+esperamos|te\s+agend[eé]|est[aá]\s+reservad[oa]\s+para\s+ti|"
    r"ya\s+qued[oó]\s+reservad[oa]|listo,?\s+quedamos)\b|"
    r"\b(?:tenemos|hay|existe)\s+disponibilidad\b.{0,70}\b(?:hoy|ma[nñ]ana|pasado\s+ma[nñ]ana|"
    r"lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|\d{1,2}\s*:\s*\d{2})\b|"
    r"\b(?:tenemos|hay)\s+(?:horarios?|horas?)\s+disponibles?\b.{0,50}\b(?:hoy|ma[nñ]ana|"
    r"pasado\s+ma[nñ]ana|esa\s+ma[nñ]ana|lunes|martes|mi[eé]rcoles|jueves|viernes|"
    r"s[aá]bado|domingo)\b|"
    r"\b(?:podemos\s+recibirte|te\s+puedo\s+recibir)\b.{0,50}\b(?:hoy|ma[nñ]ana|"
    r"lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|\d{1,2}\s*:\s*\d{2})\b|"
    r"\bhorario\s+confirmado\b)",
    re.IGNORECASE,
)

_PHONE_TARGET_RE = re.compile(
    r"(?:tel[eé]fono|celular|whatsapp|n[uú]mero\s+(?:de\s+)?contacto|n[uú]mero\s+celular)",
    re.IGNORECASE,
)

_VISIT_DAY_RE = re.compile(
    r"\b(?:hoy|ma[nñ]ana|pasado\s+ma[nñ]ana|este\s+fin\s+de\s+semana|fin\s+de\s+semana|"
    r"lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|"
    r"\d{1,2}\s*(?:de\s+)?(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|octubre|noviembre|diciembre)|\d{1,2}[/-]\d{1,2})\b",
    re.IGNORECASE,
)
_VISIT_TIME_RE = re.compile(
    r"\b(?:a\s+las?\s+\d{1,2}(?::\d{2})?|entre\s+\d{1,2}(?::\d{2})?\s+y\s+\d{1,2}(?::\d{2})?|"
    r"de\s+\d{1,2}(?::\d{2})?\s+a\s+\d{1,2}(?::\d{2})?|"
    r"\d{1,2}(?::\d{2})?\s*(?:am|pm|hrs?|horas))\b",
    re.IGNORECASE,
)
_VISIT_DAYPART_RE = re.compile(
    r"\b(?:en|por)\s+la\s+(?:ma[nñ]ana|tarde|noche)\b|\b(?:ma[nñ]ana|tarde|noche)\b",
    re.IGNORECASE,
)
_VISIT_QUESTION_RE = re.compile(
    r"(?:qu[eé]\s+d[ií]a|rango\s+horario|a\s+qu[eé]\s+hora|cu[aá]ndo).*"
    r"(?:visita|ir|ver|coordinar|agendar)|"
    r"(?:horario|hora)\s+(?:te\s+)?acomoda|"
    r"(?:coordinar|agendar)\s+(?:una\s+)?visita|"
    r"(?:te\s+)?gustar[ií]a\s+(?:coordinar|agendar|visitar)",
    re.IGNORECASE,
)


def _normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value.casefold()).strip()


def extract_visit_preference(message: str, *, visit_context: bool = False) -> str | None:
    """Extract a client-provided visit day/time without inventing availability.

    ``visit_context`` allows short replies such as ``"jueves a las 17"`` to be
    understood when the previous bot turn was already about a visit.  The
    returned value is only a preference; it never means that the visit is
    confirmed.
    """
    normalized = _normalize_text(message)
    if not normalized:
        return None
    day = _VISIT_DAY_RE.search(normalized)
    time = _VISIT_TIME_RE.search(normalized)
    daypart = _VISIT_DAYPART_RE.search(normalized)
    if not (day or time or daypart):
        return None
    if not visit_context and not is_explicit_visit_intent(normalized):
        return None

    parts = []
    for match in (day, time, daypart):
        if match:
            value = match.group(0).strip()
            if value not in parts:
                parts.append(value)
    return " ".join(parts) or None


def build_visit_progress_question(
    operation: str | None,
    *,
    financing_status: str | None = None,
    rental_docs_readiness: str | None = None,
) -> str:
    """Choose the next useful qualification question after visit timing."""
    operation_key = _normalize_text(operation)
    if operation_key in {"venta", "comprar", "compra"} and not financing_status:
        return (
            "Para que el ejecutivo pueda orientarte mejor, ¿cuentas con un crédito "
            "preaprobado, comprarías al contado o necesitas asesoría de financiamiento?"
        )
    if operation_key in {"arriendo", "arrendar", "alquilar", "alquiler"} and not rental_docs_readiness:
        return (
            "Para preparar mejor la visita, ¿ya tienes lista la documentación para el "
            "arriendo o necesitas orientación sobre los antecedentes?"
        )
    if not operation_key:
        return "Para orientar mejor la visita, ¿la propiedad la estás evaluando para compra o arriendo?"
    return "¿Hay alguna característica de la propiedad que te gustaría revisar especialmente durante la visita?"


def replace_repeated_visit_question(
    response: str,
    *,
    visit_preference: str | None,
    next_question: str | None = None,
) -> str:
    """Remove a repeated scheduling question after the client gave a preference."""
    if not visit_preference or not response:
        return response
    sentences = re.split(r"(?<=[.!?])\s+|\n+", str(response).strip())
    removed = False
    retained = []
    for sentence in sentences:
        normalized = _normalize_text(sentence)
        if "?" in sentence and _VISIT_QUESTION_RE.search(normalized):
            removed = True
            continue
        retained.append(sentence.strip())
    if not removed:
        return response
    result = "\n".join(item for item in retained if item).strip()
    if next_question and _normalize_text(next_question) not in _normalize_text(result):
        result = f"{result}\n\n{next_question}".strip()
    return result


def is_explicit_visit_intent(message: str) -> bool:
    """Detect operational visit intent without treating generic interest as a visit."""
    normalized = _normalize_text(message)
    return bool(normalized and any(pattern.search(normalized) for pattern in _VISIT_INTENT_RE))


def should_offer_visit_data(
    message: str,
    llm_intent: str | None = None,
    *,
    pending_visit_confirmation: bool = False,
    visit_data_state: dict | None = None,
    property_id: str | None = None,
) -> bool:
    """Return whether optional visit-data enrichment may be offered this turn.

    A broad LLM ``agendar_visita`` classification is intentionally insufficient;
    it must be supported by an operational phrase or a pending affirmative reply.
    """
    state = visit_data_state or {}
    same_property = not state.get("property_id") or not property_id or str(state.get("property_id")) == str(property_id)
    if same_property and (state.get("status") in {"declined", "completed"} or state.get("accepted_at")):
        return False
    normalized = _normalize_text(message)
    explicit = is_explicit_visit_intent(normalized)
    affirmative = bool(pending_visit_confirmation and _VISIT_ACCEPTANCE_RE.match(normalized))
    return bool(explicit or affirmative)


def classify_visit_data_reply(message: str, *, offer_pending: bool) -> str:
    """Classify a response to the optional data offer as accept/decline/unknown."""
    if not offer_pending:
        return "none"
    normalized = _normalize_text(message)
    if _VISIT_DECLINE_RE.match(normalized):
        return "declined"
    if _VISIT_ACCEPTANCE_RE.match(normalized):
        return "accepted"
    return "unknown"


def visit_data_fields_missing(state: dict, prospecto: dict | None = None) -> list[str]:
    """Return allowed visit fields in the requested order, excluding captured ones."""
    prospecto = prospecto or {}
    captured = set(state.get("captured_fields") or [])
    return [
        field for field in ("nombre", "rut", "email")
        if field not in captured and not prospecto.get(field)
    ]


def build_visit_data_prompt(field: str) -> str:
    prompts = {
        "nombre": "Si quieres, puedo dejar adelantados tus datos para que el ejecutivo encargado coordine la visita más rápido. Es opcional y la visita la coordina el ejecutivo. ¿Me compartes tu nombre completo?",
        "rut": "Gracias. Para dejar el dato adelantado al ejecutivo, ¿me compartes tu RUT? Es opcional; si prefieres, puedes entregárselo directamente al ejecutivo.",
        "email": "Gracias. ¿Me compartes tu correo electrónico para dejarlo adelantado al ejecutivo? Es opcional y puedes entregárselo directamente a él si prefieres.",
    }
    return prompts.get(field, "El ejecutivo podrá coordinar la visita directamente contigo.")


def visit_data_declined_response() -> str:
    return "Está bien. Dejé registrado tu interés y el ejecutivo podrá coordinar la visita directamente contigo."


def alternative_requested(message: str) -> bool:
    return bool(_ALTERNATIVE_REQUEST_RE.search(_normalize_text(message)))


def property_rejected(message: str) -> bool:
    return bool(_PROPERTY_REJECTION_RE.search(_normalize_text(message)))


def alternative_offer_accepted(message: str, *, offer_pending: bool) -> bool:
    if not offer_pending:
        return False
    normalized = _normalize_text(message)
    return bool(_VISIT_ACCEPTANCE_RE.fullmatch(normalized))


def alternative_offer_declined(message: str, *, offer_pending: bool) -> bool:
    if not offer_pending:
        return False
    normalized = _normalize_text(message)
    return bool(_VISIT_DECLINE_RE.fullmatch(normalized))


def filter_relaxation_accepted(message: str, *, offer_pending: bool) -> bool:
    if not offer_pending:
        return False
    normalized = _normalize_text(message)
    return bool(re.match(r"^(?:si|claro|dale|adelante|ok|bueno)\b", normalized)
                or re.search(r"\b(?:ampl[ií]a|ampliar|flexibiliza)\b", normalized))


def outbound_unconfirmed_visit_claim(text: str) -> bool:
    # Evaluate each sentence independently. A safe sentence must never
    # absolve a prohibited claim in another sentence of the same response.
    segments = re.split(r"(?<=[.!?])\s+|;\s*", str(text or "").strip())
    for segment in segments:
        normalized = _normalize_text(segment)
        if not normalized:
            continue
        # This is a bounded safe construction: it delegates the availability
        # check to the executive. It must not suppress a second claim in the
        # same sentence.
        safe_future_check = re.search(
            r"\b(?:el\s+)?ejecutivo\s+confirmara\s+si\s+existe\s+disponibilidad\b",
            normalized,
        )
        if safe_future_check and not re.search(
            r"\b(?:te\s+agend[eé]|agendada|confirmada|reservad[oa]|"
            r"tenemos\s+disponibilidad|hay\s+disponibilidad|podemos\s+recibirte)\b",
            normalized[safe_future_check.end():],
        ):
            continue
        if _UNCONFIRMED_VISIT_RE.search(normalized):
            return True
    return False


def safe_visit_claim_free_response(original: str) -> str:
    """Replace unsupported booking claims while retaining a useful response."""
    text = str(original or "").strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    retained = [sentence for sentence in sentences if not outbound_unconfirmed_visit_claim(sentence)]
    useful = " ".join(retained).strip()
    suffix = "Registré tu interés; el ejecutivo confirmará la disponibilidad y coordinará el horario contigo."
    return f"{useful} {suffix}".strip() if useful else suffix


def normalize_response(text: str) -> str:
    value = _normalize_text(text)
    return re.sub(r"[^a-z0-9@.]+", " ", value).strip()


def is_substantial_duplicate(candidate: str, previous: list[str] | tuple[str, ...]) -> bool:
    """Deterministic duplicate guard for recent bot messages."""
    current = normalize_response(candidate)
    if not current:
        return False
    return any(current == normalize_response(item) for item in previous if item)


def duplicate_response_fallback(original: str) -> str:
    """Return a neutral fallback without inventing a visit handoff."""
    if outbound_phone_request(original):
        return safe_phone_free_response(original)
    normalized = _normalize_text(original)
    if re.search(r"\b(?:rut|correo|email|nombre|datos)\b", normalized):
        return "Ya registré lo que me indicaste. ¿Qué otra información necesitas?"
    if re.search(r"\b(?:visita|verla|verlo|coordinar|agendar)\b", normalized):
        return "Ya registré tu interés. El ejecutivo confirmará la coordinación contigo."
    return "Gracias, sigo atento a tu consulta."


def extract_spontaneous_lead_signals(message: str, operation: str | None = None) -> dict:
    """Extract only high-confidence analytics signals already volunteered by the client."""
    normalized = _normalize_text(message)
    result = {}
    if re.search(r"\b(?:reci[eé]n\s+(?:empec[eé]|comenc[eé])|acabo\s+de\s+empezar)\b", normalized):
        result["search_duration_bucket"] = "just_started"
    elif re.search(r"\b(?:hace|llevo)\s+(?:menos\s+de\s+)?(?:un\s+mes|1\s+mes)\b", normalized):
        result["search_duration_bucket"] = "lt_1_month"
    elif re.search(r"\b(?:[12]\s*(?:a|-|y)\s*3|dos\s+a\s+tres)\s+mes(?:es)?\b", normalized):
        result["search_duration_bucket"] = "1_3_months"
    elif re.search(r"\b(?:[3-5]\s*(?:a|-|y)\s*6|tres\s+a\s+seis|cuatro|cinco|seis)\s+mes(?:es)?\b", normalized):
        result["search_duration_bucket"] = "3_6_months"
    elif re.search(r"\b(?:m[aá]s\s+de\s+6|llevo\s+(?:varios|muchos)|m[aá]s\s+de\s+seis)\s+mes(?:es)?\b", normalized):
        result["search_duration_bucket"] = "gt_6_months"

    explicit_operation = _normalize_text(operation or "")
    if not explicit_operation:
        if re.search(r"\b(?:comprar|compra|venta|vender)\b", normalized):
            explicit_operation = "venta"
        elif re.search(r"\b(?:arrendar|arriendo|alquilar|alquiler)\b", normalized):
            explicit_operation = "arriendo"

    if explicit_operation in {"venta", "comprar", "compra"} and re.search(r"\b(?:cr[eé]dito\s+)?pre\s*aprobado\b", normalized):
        result["financing_status"] = "preapproved"
    elif explicit_operation in {"venta", "comprar", "compra"} and (re.search(r"\bcr[eé]dito\b.{0,35}\b(?:evaluaci[oó]n|revisando|en\s+proceso)\b", normalized) or re.search(r"\b(?:evaluando|revisando)\b.{0,25}\bcr[eé]dito\b", normalized)):
        result["financing_status"] = "under_evaluation"
    elif explicit_operation in {"venta", "comprar", "compra"} and re.search(r"\b(?:necesito|tengo\s+que|debo)\b.{0,25}\b(?:pedir|gestionar|conseguir)\b.{0,20}\bcr[eé]dito\b", normalized):
        result["financing_status"] = "needs_financing"
    elif explicit_operation in {"venta", "comprar", "compra"} and re.search(r"\b(?:al\s+contado|contado|efectivo)\b", normalized):
        result["financing_status"] = "cash"

    if explicit_operation in {"arriendo", "arrendar", "alquilar", "alquiler"} and re.search(r"\b(?:tengo|ya\s+tengo)\b.{0,35}\b(?:todos?\s+los\s+)?(?:documentos|papeles|antecedentes)\b", normalized):
        result["rental_docs_readiness"] = "ready"
    elif explicit_operation in {"arriendo", "arrendar", "alquilar", "alquiler"} and re.search(r"\b(?:me\s+faltan|tengo\s+algunos|parcialmente)\b.{0,35}\b(?:documentos|papeles|antecedentes)\b", normalized):
        result["rental_docs_readiness"] = "partially_ready"
    elif explicit_operation in {"arriendo", "arrendar", "alquilar", "alquiler"} and re.search(r"\b(?:no\s+tengo|me\s+faltan\s+todos)\b.{0,35}\b(?:documentos|papeles|antecedentes)\b", normalized):
        result["rental_docs_readiness"] = "not_ready"
    return result


def outbound_phone_request(text: str) -> bool:
    """True only for an explicit request, never for ordinary contact mentions."""
    # Covers requests such as “para coordinar necesito tu número” and “envíame
    # el celular”, while leaving executive/property contact references intact.
    request_context = re.compile(
        r"\b(?:necesito|requiero|dame|env[ií]ame|m[aá]ndame|comparte|ind[ií]came|"
        r"p[aá]same|deja(?:me)?|puedes?\s+(?:darme|compartir)|cu[aá]l\s+es)\b"
        r".{0,90}\b(?:tel[eé]fono|celular|whatsapp|n[uú]mero\s+(?:de\s+)?contacto|"
        r"n[uú]mero\s+celular)\b",
        re.IGNORECASE | re.DOTALL,
    )

    segments = re.split(r"(?<=[.!?])\s+|;\s*", str(text or "").strip())
    for segment in segments:
        normalized = _normalize_text(segment)
        if not normalized:
            continue
        # A negative mention is safe only when that same clause does not also
        # contain a later request. This prevents mixed messages from bypassing
        # the guard through a global exception.
        negative = re.match(
            r"^no\s+(?:necesito|quiero|hace\s+falta)\b.*?"
            r"(?:tel[eé]fono|n[uú]mero|celular|whatsapp)\b\s*",
            normalized,
            re.IGNORECASE,
        )
        if negative:
            remainder = normalized[negative.end():]
            if not (_PHONE_REQUEST.search(remainder) or request_context.search(remainder)):
                continue
        if _PHONE_REQUEST.search(normalized) or request_context.search(normalized):
            return True
    return False


def safe_phone_free_response(original: str) -> str:
    """Remove the prohibited request while retaining any useful answer text."""
    parts = re.split(r"(?<=[.!?])\s+", str(original or "").strip())
    retained = [part for part in parts if not outbound_phone_request(part)]
    useful = " ".join(retained).strip()
    follow_up = "¿Qué día o rango horario te acomoda más?"
    if not useful:
        return f"Para avanzar con la coordinación, {follow_up[0].lower()}{follow_up[1:]}"
    if "?" in useful:
        return useful
    return f"{useful} {follow_up}"


def nudge_eligibility(lead: dict) -> dict:
    """Return a decision plus auditable reason without relying on response text."""
    status = str(lead.get("conversation_status") or "")
    stage = str(lead.get("stage") or lead.get("pipeline_stage") or "").upper()
    last_intent = str(lead.get("last_intent") or "").upper()
    pending = lead.get("pending_response") or {}
    if status == "BLOCKED_EXTERNAL_BROKER":
        return {"eligible": False, "reason": "blocked_external_broker", "evidence": status, "state": status}
    if status in {"STOPPED_BY_CLIENT", "CLOSED", "HUMAN_HANDOFF"}:
        return {"eligible": False, "reason": "conversation_status", "evidence": status, "state": status}
    if stage in {"ARCHIVED", "REJECTED", "CLOSED_LOST", "CLOSED_WON", "VISIT_DONE", "VISIT_SCHEDULED"}:
        return {"eligible": False, "reason": "terminal_stage", "evidence": stage, "state": stage}
    # Intent, an executive assignment and an alert are not proof that a person
    # has taken over.  Only a canonical status/timestamp stops the nudge.
    takeover_at = lead.get("human_takeover_at") or (lead.get("lifecycle") or {}).get("human_takeover_at")
    if takeover_at:
        return {"eligible": False, "reason": "human_takeover_confirmed", "evidence": takeover_at, "state": "human_handoff"}
    if lead.get("bot_pausado") or lead.get("delivery_unknown_pending"):
        return {"eligible": False, "reason": "manual_pause_or_delivery_unknown", "evidence": True, "state": status}
    messages = lead.get("messages") or []
    last_message = messages[-1] if messages else {}
    if last_message.get("role") == "assistant" and last_message.get("tipo") in {
        "rechazo_corredor", "cierre_conversacion", "despedida",
    }:
        return {"eligible": False, "reason": "terminal_bot_message", "evidence": last_message.get("tipo"), "state": status}
    return {"eligible": True, "reason": "eligible", "evidence": None, "state": status or stage}
