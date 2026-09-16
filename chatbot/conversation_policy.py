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
    r"\b(?:puedo|podr[ií]a)\s+ir(?:\s+(?:tipo|a\s+las?)\s+\d{1,2}(?::\d{2})?)?\b",
    r"\bse\s+pu[eé]d(?:e)?\s+ver(?:la|lo)?\b",
    r"\b(?:puedo|podr[ií]a)\s+ir\s+(?:ma[nñ]ana|hoy|el\s+(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo))\b",
    r"\b(?:puedo|podr[ií]a)\s+(?:visitar|ver(?:la|lo)?)\b",
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
    r"(?:tipo\s+|a\s+las?\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm|hrs?|horas)|"
    r"tipo\s+\d{1,2}(?::\d{2})?)\b",
    re.IGNORECASE,
)

_PROPERTY_IDENTIFIER_RE = re.compile(
    r"(?:https?://|www\.)|"
    r"\b(?:c[oó]digo|c[oó]d|id|ref(?:erencia)?|folio)\s*[:#-]?\s*[a-z0-9][a-z0-9_-]{2,}\b|"
    r"\b(?:calle|avenida|av\.?|pasaje|camino|ruta)\s+[a-záéíóúñü0-9]|"
    r"\b[a-záéíóúñü]{3,}\s+con\s+[a-záéíóúñü]{3,}\b",
    re.IGNORECASE,
)
_VISIT_DATE_RANGE_RE = re.compile(
    r"\b(?:desde\s+(?:el\s+)?|a\s+partir\s+del\s+|a\s+contar\s+del\s+)\d{1,2}\s+en\s+adelante\b",
    re.IGNORECASE,
)
_NEAR_TERM_VISIT_RE = re.compile(
    r"\b(?:hoy|ma[nñ]ana|pasado\s+ma[nñ]ana|esta\s+(?:ma[nñ]ana|tarde|noche)|"
    r"este\s+fin\s+de\s+semana|fin\s+de\s+semana|lunes|martes|mi[eé]rcoles|jueves|"
    r"viernes|s[aá]bado|domingo)\b",
    re.IGNORECASE,
)

_FALLBACK_AVAILABILITY_RE = re.compile(
    r"\b(?:disponible|disponibilidad|dispnible|disponble|sigue\s+disponible|a[uú]n\s+est[aá])\b",
    re.IGNORECASE,
)
_FALLBACK_PRICE_RE = re.compile(
    r"\b(?:precio|valor|cu[aá]nto\s+(?:vale|cuesta|sale)|cu[aá]nto\s+es|uf)\b",
    re.IGNORECASE,
)
_PRICE_NEGOTIATION_RE = re.compile(
    r"\b(?:se\s+puede\s+(?:bajar|negociar|rebajar)|podemos\s+(?:negociar|bajar)|"
    r"aceptan\s+ofertas?|hacer\s+una\s+oferta|descuento|rebaja|est[aá]\s+muy\s+car[oa]|ta\s+car[oa])\b",
    re.IGNORECASE,
)
_PRICE_INCLUSIONS_RE = re.compile(
    r"\b(?:incluye|incluyen|incluido|inclu[ií]do|considera|contempla)\b.{0,70}\b(?:"
    r"servicios?\s+b[aá]sicos?|gastos?\s+comunes?|luz|agua|gas|internet)\b|"
    r"\b(?:servicios?\s+b[aá]sicos?|gastos?\s+comunes?)\b.{0,70}\b(?:incluye|incluido|"
    r"considera|contempla)\b",
    re.IGNORECASE,
)
_COMMON_EXPENSES_RE = re.compile(
    r"\b(?:gastos?\s+comunes?|gc|qu[eé]\s+gastos?\s+t(?:ien|iene|ienen))\b",
    re.IGNORECASE,
)
_PROPERTY_ATTRIBUTE_RE = re.compile(
    r"\b(?:orientaci[oó]n|dormitorios?|habitaciones?|ba[nñ]os?|superficie|metros?|"
    r"estacionamientos?|estac\w*|bodegas?|antig[uü]edad)\b",
    re.IGNORECASE,
)
_VISIT_DAYPART_RE = re.compile(
    r"\b(?:en|por)\s+la\s+(?:ma[nñ]ana|tarde|noche)\b|\b(?:ma[nñ]ana|tarde|noche)\b",
    re.IGNORECASE,
)
_VISIT_QUESTION_RE = re.compile(
    r"(?:qu[eé]\s+d[ií]a|rango\s+horario|a\s+qu[eé]\s+hora|cu[aá]ndo).*"
    r"(?:visita|ir|ver|coordinar|agendar)|"
    r"(?:qu[eé]\s+(?:d[ií]a|horario|hora)).{0,35}\b(?:acomoda|conviene|puedes|tienes\s+disponibilidad)\b|"
    r"(?:horario|hora)\s+(?:te\s+)?acomoda|"
    r"(?:coordinar|agendar)\s+(?:una\s+)?visita|"
    r"(?:te\s+)?gustar[ií]a\s+(?:coordinar|agendar|visitar)",
    re.IGNORECASE,
)
_VISIT_SCHEDULE_CHANGE_RE = re.compile(
    r"\b(?:mejor|en\s+vez(?:\s+de)?)\s+(?:el\s+)?"
    r"(?:hoy|ma[nñ]ana|pasado\s+ma[nñ]ana|lunes|martes|mi[eé]rcoles|jueves|"
    r"viernes|s[aá]bado|domingo)(?:\b|\s+)",
    re.IGNORECASE,
)
_OWNER_SERVICE_RE = re.compile(
    r"\b(?:tengo\s+(?:una\s+)?(?:propiedad|casa|departamento|depto|local)|"
    r"quiero\s+(?:vender|arrendar|alquilar|publicar)\s+mi|"
    r"busco\s+(?:una\s+)?corredora|quiero\s+que\s+ustedes\s+la\s+"
    r"(?:arrienden|vendan)|capt(?:ar|aci[oó]n)|sin\s+exclusividad|"
    r"comisi[oó]n)\b",
    re.IGNORECASE,
)

# Short acknowledgements are not a conversational turn that needs an LLM.
# Keep this allow-list deliberately conservative: any question, date, URL,
# contact detail, property signal or operational verb is treated as new
# information and must continue through the normal pipeline.
_ACK_ONLY_PHRASES = frozenset({
    "ok", "okay", "oki", "gracias", "muchas gracias", "quedo atento",
    "gracias quedo atento", "muchas gracias quedo atento",
    "perfecto", "perfecto gracias", "bueno gracias", "listo", "dale",
    "de acuerdo", "entendido", "👍", "👌", "✅", "🙏",
})
_ACK_ONLY_NEW_INFORMATION_RE = re.compile(
    r"(?:\?|¿|https?://|www\.|\b(?:visita|visitar|ver|ir|disponib|\d{1,2}\s*:\s*\d{2}|"
    r"hoy|ma[nñ]ana|jueves|viernes|lunes|martes|mi[eé]rcoles|s[aá]bado|domingo|"
    r"correo|email|mail|gasto|habitacional|valor|precio|c[oó]digo|direcci[oó]n|"
    r"propiedad|local|oficina|departamento|casa|tengo|puedo|quiero|necesito)\b|"
    r"\b\d{2,}\b|@)",
    re.IGNORECASE,
)


def _normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value.casefold()).strip()


def is_acknowledgement_only(message: str) -> bool:
    """Return True only for a pure acknowledgement with no new intent.

    This gate runs before DeepSeek. It deliberately does not infer sentiment
    or intent: it only recognizes a small set of closing/waiting phrases and
    rejects anything that could carry a question, visit timing, property
    identity or contact/qualification data.
    """
    raw = str(message or "").strip()
    normalized = _normalize_text(raw)
    if not normalized or _ACK_ONLY_NEW_INFORMATION_RE.search(raw):
        return False
    # Remove common acknowledgement emoji and punctuation while retaining
    # words, then compare the complete turn rather than a substring.
    cleaned = re.sub(r"[👍👌✅🙏🙂😊😉🙌👏❤️❤]+", " ", normalized)
    cleaned = re.sub(r"[^\wáéíóúñü]+", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned in _ACK_ONLY_PHRASES:
        return True
    # Emoji-only messages become empty after normalization.
    return not cleaned and bool(re.search(r"[👍👌✅🙏🙂😊😉🙌👏❤️❤]", raw))


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
    if _OWNER_SERVICE_RE.search(normalized):
        return None
    schedule_change = _VISIT_SCHEDULE_CHANGE_RE.search(normalized)
    scoped = normalized[schedule_change.start():] if schedule_change else normalized
    days = list(_VISIT_DAY_RE.finditer(scoped))
    date_ranges = list(_VISIT_DATE_RANGE_RE.finditer(scoped))
    times = list(_VISIT_TIME_RE.finditer(scoped))
    dayparts = list(_VISIT_DAYPART_RE.finditer(scoped))
    if not (days or date_ranges or times or dayparts):
        return None
    if not visit_context and not is_explicit_visit_intent(normalized):
        return None

    parts = []
    matches = sorted(days + date_ranges + times + dayparts, key=lambda match: match.start())
    # ``_VISIT_DAY_RE`` also recognizes "mañana", while the day-part matcher
    # recognizes the more informative phrase "en la mañana". Keep the
    # containing match and avoid duplicating the same temporal signal.
    matches = [
        match for match in matches
        if not any(
            other is not match
            and other.start() <= match.start()
            and match.end() <= other.end()
            and (other.start() < match.start() or other.end() > match.end())
            for other in matches
        )
    ]
    for match in matches:
        value = match.group(0).strip()
        if value not in parts:
            parts.append(value)
    # A customer may provide several bare hours: "viernes 11, 14:15 o 16".
    # They are valid only in visit context and only when a day/date exists.
    # Reject values attached to property facts so m², floor, bedrooms,
    # installments, UF, prices and export timestamps cannot become visit data.
    if days and (visit_context or is_explicit_visit_intent(normalized)):
        structured_spans = [(match.start(), match.end()) for match in matches]
        for match in re.finditer(r"(?<![\w/:])(?:[01]?\d|2[0-3])(?::[0-5]\d)?(?![\w/:])", scoped):
            token = match.group(0)
            if any(start <= match.start() and match.end() <= end for start, end in structured_spans):
                continue
            before = scoped[max(0, match.start() - 14):match.start()]
            after = scoped[match.end():match.end() + 14]
            if re.search(r"(?:\$|uf|m2|m²|piso|dormitorio|baño|cuota)\s*$", before, re.I):
                continue
            if re.match(r"\s*(?:m2|m²|uf|cuotas?|dormitorios?|baños?|piso)\b", after, re.I):
                continue
            if token not in parts:
                parts.append(token)
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


def contains_property_identifier(message: str) -> bool:
    """Return whether the current turn contains a usable property identifier signal."""
    return bool(_PROPERTY_IDENTIFIER_RE.search(str(message or "")))


def has_near_term_visit_urgency(message: str) -> bool:
    """Detect a near-term date/daypart that makes a visit request operationally urgent."""
    return bool(_NEAR_TERM_VISIT_RE.search(_normalize_text(message)))


def property_identifier_action(
    *,
    visit_requested: bool,
    property_resolved: bool,
    awaiting_identifier: bool,
    identifier_in_message: bool,
) -> str:
    """Choose the deterministic property-identity gate action for a visit turn."""
    if property_resolved and (not awaiting_identifier or identifier_in_message):
        return "resolved"
    if awaiting_identifier and visit_requested and not identifier_in_message:
        # The identifier question was already sent.  A subsequent timing
        # reply should be acknowledged and preserved, not asked again.
        return "acknowledge"
    if awaiting_identifier:
        return "clarify"
    if visit_requested and not property_resolved:
        return "ask"
    return "none"


def build_property_identifier_request() -> str:
    return (
        "Para poder coordinarte la visita lo antes posible, ¿me puedes enviar el enlace "
        "de la publicación de la propiedad que quieres visitar? Así identifico exactamente "
        "cuál es y revisamos disponibilidad para el día que necesitas."
    )


def build_property_identifier_clarification() -> str:
    return (
        "Para continuar con la visita necesito identificar la propiedad. ¿Me compartes "
        "el enlace, la dirección o el código de la publicación?"
    )


def build_pending_visit_preference_acknowledgement() -> str:
    """Acknowledge timing without repeating an already-sent link request."""
    return (
        "Perfecto, dejo registrada tu disponibilidad. Solo me falta el enlace de la "
        "publicación para identificar exactamente la propiedad y continuar con la coordinación."
    )


def _display_visit_preference(preference: str | None) -> str:
    """Restore the most common Spanish accents removed by normalization."""
    value = re.sub(r"\s+", " ", str(preference or "")).strip()
    replacements = {
        "manana": "mañana",
        "miercoles": "miércoles",
        "sabado": "sábado",
    }
    for source, target in replacements.items():
        value = re.sub(rf"\b{source}\b", target, value, flags=re.IGNORECASE)
    return value


def build_visit_scheduling_request() -> str:
    """Ask for a visit time after the property is already identified."""
    return (
        "Sí, podemos solicitar una visita. ¿Qué día y horario te acomodaría? "
        "La disponibilidad debe confirmarse antes de agendar."
    )


def build_visit_preference_confirmation(preference: str | None) -> str:
    """Confirm a preference without claiming that the visit is booked."""
    display = _display_visit_preference(preference) or "ese horario"
    return (
        f"Perfecto, dejo registrada tu preferencia para {display}. "
        "La disponibilidad debe confirmarse antes de agendar."
    )


def classify_local_fallback_intent(message: str, *, operational_intent: str | None = None) -> str:
    """Classify a local fallback without invoking an LLM.

    The classifier is deliberately conservative: it only selects an
    operational category when the customer's own text provides a strong
    signal.  In particular, availability alone is not treated as a visit
    request, so the fallback never claims a booking or a confirmed slot.
    """
    normalized = _normalize_text(message)
    if is_explicit_visit_intent(normalized) or str(operational_intent or "").casefold() in {"agendar_visita", "ask_visit"}:
        return "ASK_VISIT"
    if _PRICE_NEGOTIATION_RE.search(normalized):
        return "PRICE_NEGOTIATION"
    if _PRICE_INCLUSIONS_RE.search(normalized):
        return "PRICE_INCLUSIONS"
    if _COMMON_EXPENSES_RE.search(normalized):
        return "COMMON_EXPENSES"
    if _PROPERTY_ATTRIBUTE_RE.search(normalized):
        return "PROPERTY_ATTRIBUTE"
    if _FALLBACK_PRICE_RE.search(normalized):
        return "PRICE_CURRENT_VALUE"
    if _FALLBACK_AVAILABILITY_RE.search(normalized):
        return "ASK_AVAILABILITY"
    return "GENERAL"


def classify_local_semantic_intents(message: str) -> tuple[str, ...]:
    """Return all deterministic sub-intents present in one inbound burst."""
    normalized = _normalize_text(message)
    intents = []
    if _PRICE_NEGOTIATION_RE.search(normalized):
        intents.append("PRICE_NEGOTIATION")
    if _PRICE_INCLUSIONS_RE.search(normalized):
        intents.append("PRICE_INCLUSIONS")
    elif _COMMON_EXPENSES_RE.search(normalized):
        intents.append("COMMON_EXPENSES")
    if _PROPERTY_ATTRIBUTE_RE.search(normalized):
        intents.append("PROPERTY_ATTRIBUTE")
    if _FALLBACK_PRICE_RE.search(normalized) and not any(
        item in intents for item in {"PRICE_NEGOTIATION", "PRICE_INCLUSIONS", "COMMON_EXPENSES"}
    ):
        intents.append("PRICE_CURRENT_VALUE")
    if is_explicit_visit_intent(normalized):
        intents.append("ASK_VISIT")
    if _FALLBACK_AVAILABILITY_RE.search(normalized):
        intents.append("ASK_AVAILABILITY")
    return tuple(dict.fromkeys(intents))


def build_local_fallback_response(
    message: str,
    *,
    operational_intent: str | None = None,
    property_facts: dict | None = None,
    intent_override: str | None = None,
) -> tuple[str, str]:
    """Build a safe deterministic fallback and return ``(text, intent)``.

    Only facts explicitly present in ``property_facts`` are rendered. Unknown
    facts remain explicitly unknown; the fallback never invents a value.
    """
    facts = property_facts or {}

    def _nested(*paths):
        for path in paths:
            value = facts
            try:
                for key in path:
                    value = value[key]
            except (KeyError, TypeError):
                value = None
            if value not in (None, "", 0, False):
                return value
        return None

    operation = str(facts.get("operacion") or "").casefold()
    price_block_paths = (
        ("tipo_operacion", "precio_venta") if operation in {"venta", "comprar", "compra"}
        else ("tipo_operacion", "precio_arriendo") if operation in {"arriendo", "arrendar", "alquilar", "alquiler"}
        else ()
    )
    price_paths = [price_block_paths] if price_block_paths else []
    price_uf = _nested(("precio_uf",), *(path + ("precio_uf",) for path in price_paths))
    price_clp = _nested(("precio_clp",), *(path + ("precio_clp",) for path in price_paths))
    common_expenses = _nested(("gastos_comunes",), ("tipo_operacion", "gastos_comunes"))
    orientation = _nested(("orientacion",), ("caracteristicas", "orientacion"))

    def _number(value, *, prefix=""):
        if value is None:
            return None
        try:
            numeric = float(value)
            if numeric.is_integer():
                rendered = f"{int(numeric):,}".replace(",", ".")
            else:
                rendered = f"{numeric:.2f}".rstrip("0").rstrip(".").replace(".", ",")
            return f"{prefix}{rendered}"
        except (TypeError, ValueError):
            return str(value)

    price_display = _number(price_uf, prefix="")
    if price_display is not None:
        price_display = f"{price_display} UF"
    elif price_clp is not None:
        price_display = _number(price_clp, prefix="$" )
    intent = intent_override or classify_local_fallback_intent(
        message, operational_intent=operational_intent,
    )
    responses = {
        "ASK_VISIT": (
            "Gracias por escribirnos. Podemos ayudarte a coordinar una visita. "
            "Vamos a confirmar la disponibilidad de la propiedad para continuar "
            "con la coordinación."
        ),
        "ASK_AVAILABILITY": (
            "Gracias por tu consulta. Vamos a confirmar si la propiedad continúa "
            "disponible y te ayudaremos con la información."
        ),
        "PRICE_CURRENT_VALUE": (
            f"El valor publicado es {price_display}. Si quieres, puedo ayudarte con los "
            "antecedentes de la propiedad." if price_display else
            "El valor publicado no aparece confirmado en la ficha. Lo podemos consultar "
            "para entregarte la información correcta."
        ),
        "PRICE_NEGOTIATION": (
            f"El valor publicado es {price_display}. Si quieres hacer una oferta, podemos "
            "transmitirla al ejecutivo o propietario para que la evalúen; no puedo garantizar "
            "que sea aceptada." if price_display else
            "Podemos transmitir una oferta al ejecutivo o propietario para que la evalúen, "
            "pero no puedo garantizar que sea aceptada."
        ),
        "PRICE_INCLUSIONS": (
            f"La ficha indica que los gastos comunes son { _number(common_expenses, prefix='$') }. "
            "Sobre los servicios básicos, revisaremos si están incluidos." if common_expenses is not None else
            "Ese dato no aparece confirmado en la ficha. Lo podemos consultar con el ejecutivo "
            "encargado para darte la información correcta."
        ),
        "COMMON_EXPENSES": (
            f"Los gastos comunes informados son { _number(common_expenses, prefix='$') }." if common_expenses is not None else
            "El valor de los gastos comunes no aparece confirmado en la ficha. Lo podemos "
            "consultar con el ejecutivo encargado."
        ),
        "PROPERTY_ATTRIBUTE": (
            f"La orientación informada en la ficha es {orientation}." if orientation is not None else
            "Ese dato no aparece confirmado en la ficha. Lo podemos consultar con el ejecutivo "
            "encargado para darte la información correcta."
        ),
        "GENERAL": (
            "Gracias por escribirnos. Recibimos tu consulta y la estamos revisando "
            "para poder ayudarte con la información de la propiedad."
        ),
    }
    return responses[intent], intent


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
    suffix = "Podemos continuar con la coordinación; la disponibilidad debe confirmarse antes de agendar."
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
        return "Podemos continuar con la coordinación; la disponibilidad debe confirmarse antes de agendar."
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
    if lead.get("conversation_owner") == "human" or lead.get("human_active") is True:
        return {"eligible": False, "reason": "human_ownership_active", "evidence": True, "state": "human_handoff"}
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
