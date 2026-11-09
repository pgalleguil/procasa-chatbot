"""Campaign-specific response intent, separate from canonical CRM temperature.

Classification is conservative and clause-aware: a preference against calls or
visits does not become a request for that action, and asking a factual question
after declining a visit does not close the property conversation.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .lead_temperature import HOT_INTENTS

CLASSIFIER_VERSION = "price_drop_response_rules_v4"
PRIORITY = "PRIORIDAD_COMERCIAL"
INTEREST = "INTERES_DECLARADO"
INFO = "CONSULTA_INFORMATIVA"
NO_SIGNAL = "SIN_SENAL"
DECLINED = "RECHAZADO"
UNSUBSCRIBED = "BAJA"
ALTERNATIVES = "BUSCA_ALTERNATIVAS"


_COMMON_TYPOS = {
    "q": "que",
    "kiero": "quiero", "qiero": "quiero", "quero": "quiero",
    "resivir": "recibir", "recivir": "recibir", "resibir": "recibir",
    "resiviendo": "recibiendo", "reciviendo": "recibiendo",
    "infomacion": "informacion", "infromacion": "informacion",
    "informasion": "informacion", "informacoin": "informacion",
    "escrivir": "escribir", "escriviendo": "escribiendo",
    "escrivirme": "escribirme", "escrivan": "escriban",
    "escriva": "escriba", "escriveme": "escribeme",
    "yamar": "llamar", "yamame": "llamame", "yamen": "llamen",
    "yamando": "llamando", "yamada": "llamada", "yamadas": "llamadas",
    "busqeda": "busqueda", "busquedaa": "busqueda",
    "vivenda": "vivienda", "viviendaa": "vivienda",
    "alternatibas": "alternativas", "altrernativas": "alternativas",
    "comnues": "comunes", "comunees": "comunes",
    "gastoscomunes": "gastos comunes",
}


def _fold(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", str(text or "").lower())
        if not unicodedata.combining(ch)
    )


def _plain(text: str) -> str:
    tokens = re.sub(r"[^a-z0-9]+", " ", _fold(text)).split()
    normalized = []
    for token in tokens:
        replacement = _COMMON_TYPOS.get(token, token)
        normalized.extend(replacement.split())
    return " ".join(normalized)


def _clauses(text: str) -> list[str]:
    folded = _fold(text)
    parts = re.split(
        r"\b(?:pero|sin embargo|no obstante|aunque|sino|mas bien)\b|[,;.!?\n]+",
        folded,
    )
    return [normalized for part in parts if (normalized := _plain(part))]


_STOP = re.compile(
    r"\b(?:stop|baja|cancel(?:a|en|ar)\s+(?:mi\s+)?suscripci[oó]n|"
    r"s[aá]came\s+de\s+la\s+lista|s[aá]quenme\s+de\s+la\s+lista|"
    r"elimina(?:r|n)?\s+(?:mi\s+)?(?:n[uú]mero|contacto|datos)|"
    r"borra(?:r|n)?\s+(?:mi\s+)?(?:n[uú]mero|contacto|datos)|"
    r"no\s+(?:m[aá]s|quiero\s+m[aá]s)\s+(?:mensajes?|informaci[oó]n|contacto|comunicaciones?)|"
    r"no\s+(?:quiero|deseo|necesito)\s+(?:recibir\s+)?(?:m[aá]s\s+)?"
    r"(?:mensajes?|informaci[oó]n|novedades?|actualizaciones?|comunicaciones?|"
    r"promociones?|publicidad|ofertas?)\b|"
    r"no\s+(?:quiero|deseo|necesito|acepto)\s+(?:seguir\s+)?"
    r"(?:recibir|recibiendo|tener|que\s+me\s+informen)\b.{0,55}\b"
    r"(?:mensajes?|informaci[oó]n|novedades?|noticias|actualizaciones?|comunicaciones?|"
    r"promociones?|publicidad|ofertas?|contacto)\b|"
    r"no\s+(?:me\s+)?(?:interesa|intereso)\s+(?:seguir\s+)?"
    r"(?:recibiendo|recibir|en\s+contacto|saber\s+m[aá]s|"
    r"que\s+me\s+(?:escrib\w*|contact\w*|envi\w*|mand\w*))\b|"
    r"no\s+(?:quiero|deseo|necesito)\s+que\s+(?:me\s+)?(?:sigan|contin[uú]en|vuelvan)\s+"
    r"(?:a\s+)?(?:escrib\w*|contact\w*|envi\w*|mand\w*|molest\w*)\b|"
    r"no\s+(?:quiero|deseo|necesito)\s+que\s+(?:me|nos)\s+"
    r"(?:escrib\w*|contact\w*|envi\w*|mand\w*|molest\w*)\b|"
    r"no\s+(?:quiero|deseo)\s+que\s+(?:se\s+)?comuniquen\s+conmigo\b|"
    r"no\s+(?:me|nos)\s+(?:sigan|contin[uú]en|vuelvan)\s+"
    r"(?:a\s+)?(?:escrib\w*|contact\w*|envi\w*|mand\w*|molest\w*)\b|"
    r"no\s+(?:me|nos)\s+(?:escrib\w*|contact\w*|envi\w*|mand\w*|molest\w*)\b|"
    r"(?:dejen|deja|paren|par[aá])\s+de\s+(?:escrib\w*|contact\w*|envi\w*|mand\w*|molest\w*)\b|"
    r"basta\s+(?:ya\s+)?de\s+(?:mensajes?|informaci[oó]n|contact\w*|comunicaci\w*|"
    r"escrib\w*|ofert\w*)\b|"
    r"no\s+(?:quiero|deseo|necesito)\s+(?:seguir\s+)?"
    r"(?:conversando|conversar|en\s+contacto|en\s+(?:esta|la)\s+conversaci[oó]n|"
    r"continu\w*\s+(?:en\s+)?(?:esta\s+|la\s+)?conversaci[oó]n|"
    r"recibiendo\s+(?:mensajes?|informaci[oó]n))\b|"
    r"(?:prefiero\s+que\s+no|por\s+favor\s+no)\s+(?:me|nos)\s+"
    r"(?:escrib\w*|contact\w*|envi\w*|mand\w*)\b|"
    r"no\s+quiero\s+saber\s+nada\s+m[aá]s)\b",
    re.IGNORECASE,
)

_ALREADY_FOUND = re.compile(
    r"\b(?:encontr[eé]|encontramos|compr[eé]|compramos|consegu[ií]|conseguimos|"
    r"arrend[eé]|arrendamos|adquir[ií]|adquirimos|me\s+adjudicaron)\b.{0,65}\b"
    r"(?:casa|propiedad|departamento|depto|vivienda|hogar|inmueble)\b|"
    r"\b(?:ya\s+)?(?:tengo|consegu[ií]|compr[eé]|arrend[eé])\s+"
    r"(?:(?:un|una|mi|otro|otra|el|la)\s+)?"
    r"(?:casa|propiedad|departamento|depto|vivienda|hogar|inmueble)\b|"
    r"\b(?:termin[eé]|finalic[eé]|cerr[eé]|complet[eé]|resolv[ií])\s+"
    r"(?:(?:por\s+fin|ya)\s+)?(?:(?:mi|la)\s+)?b[uú]squeda\b|"
    r"\b(?:mi|la)\s+b[uú]squeda\s+(?:de\s+vivienda\s+)?"
    r"(?:ya\s+)?(?:termin[oó]|finaliz[oó]|se\s+acab[oó]|est[aá]\s+lista|qued[oó]\s+resuelta)\b",
    re.IGNORECASE,
)

_HYPOTHETICAL_FOUND = re.compile(
    r"\b(?:y\s+si|que\s+pasar[ií]a\s+si|imagina(?:te)?\s+que|"
    r"si\s+alg[uú]n\s+d[ií]a)\b.{0,45}\b"
    r"(?:encontr[eé]|compr[eé]|consegu[ií]|arrend[eé]|termin[eé]|finalic[eé])\b",
    re.IGNORECASE,
)
_NEGATED_FOUND = re.compile(
    r"\b(?:(?:no|todav[ií]a\s+no|a[uú]n\s+no)\s+(?:he\s+)?"
    r"(?:encontr\w*|consegu\w*|compr\w*|arrend\w*|adquir\w*))\b",
    re.IGNORECASE,
)

_OPT_OUT_ONLY = re.compile(
    r"^(?:stop|baja|cancelar|borrame|eliminame|sacame\s+de\s+la\s+lista|"
    r"no\s+mas|no\s+quiero\s+mas|no\s+me\s+escribas?)$"
)
_CALL_TARGET = re.compile(
    r"\b(?:llam\w*|llamada\w*|telefono\w*|telef[oó]nic\w*|contacto\s+telef[oó]nico)\b"
)
_NEGATED_CALL = re.compile(
    r"\b(?:no\s+(?:(?:me|nos)\s+)?(?:quiero|deseo|necesito|acepto|"
    r"prefiero|gusta|gustar[ií]a|puedo|podr[ií]a)?|"
    r"prefiero\s+que\s+no|prefiero\s+no|no\s+(?:me|nos)|sin)\b.{0,42}"
    r"\b(?:llam\w*|llamada\w*|telefono\w*|telef[oó]nic\w*)\b|"
    r"\bno\s+(?:quiero|deseo|necesito|acepto)\s+(?:una\s+)?"
    r"(?:llamada|llamadas|contacto\s+telef[oó]nico)\b",
    re.IGNORECASE,
)
_NEGATED_VISIT = re.compile(
    r"\b(?:(?:no\s+(?:(?:me\s+)?(?:interesa|interesar[ií]a|gusta|gustar[ií]a|"
    r"quiero|quisiera|deseo|necesito|puedo|podr[ií]a|pienso|planeo)))|"
    r"(?:no\s+estoy\s+interesad[oa])|"
    r"(?:nunca\s+(?:quiero|quisiera|pienso|planeo))|"
    r"(?:jam[aá]s\s+(?:quiero|quisiera))|(?:prefiero\s+no)|(?:mejor\s+no))\b.{0,48}"
    r"\b(?:visita\w*|visit\w*|agend\w*|coordin\w*|ver(?:la|lo)|"
    r"ver\s+(?:la|el|esta|esa)\s+(?:propiedad|casa|vivienda|departamento|depto)|"
    r"ir\s+a\s+ver\s+(?:(?:la|el|esta|esa)\s+)?"
    r"(?:propiedad|casa|vivienda|departamento|depto)|"
    r"conocer(?:la|lo)|conocer\s+(?:la|el|esta|esa)\s+"
    r"(?:propiedad|casa|vivienda|departamento|depto))\b|"
    r"\bno\s+(?:quiero|deseo|necesito)\s+(?:una\s+)?visita\b",
    re.IGNORECASE,
)
_NEGATED_INTEREST = re.compile(
    r"\b(?:no\s+(?:me\s+)?(?:interesa|intereso|estoy\s+interesad[oa]|"
    r"me\s+gusta|quiero)|ya\s+no\s+me\s+interesa)\b",
    re.IGNORECASE,
)
_NEGATED_OFFER = re.compile(
    r"\b(?:no\s+(?:quiero|quisiera|puedo|podr[ií]a|voy\s+a|deseo|"
    r"pienso|pretendo)|prefiero\s+no)\b.{0,55}\b"
    r"(?:ofert\w*|ofrec\w*|hacer\s+una\s+oferta|propon\w*\s+pagar)\b",
    re.IGNORECASE,
)
_NEGATED_ALTERNATIVE = re.compile(
    r"\b(?:no\s+(?:busco|estoy\s+buscando|quiero\s+buscar|quisiera\s+buscar|"
    r"necesito\s+buscar|quiero|necesito|me\s+interesa|me\s+interesan|"
    r"quisiera|deseo|prefiero\s+buscar)|prefiero\s+no\s+buscar)\b.{0,65}\b"
    r"(?:otra\s+propiedad|otro\s+departamento|otras?\s+(?:casas|propiedades|"
    r"opciones|alternativas|comunas)|otra\s+comuna|comunas\s+distintas|"
    r"(?:buscar\s+)?(?:en|por)\s+[a-z]{3,}|algo\s+(?:en|por)\s+[a-z]{3,}|"
    r"m[aá]s\s+(?:propiedades|opciones|alternativas))\b",
    re.IGNORECASE,
)

_ALTERNATIVES = re.compile(
    r"\b(?:busco|buscando|quiero|quisiera|necesito|me\s+gustar[ií]a|"
    r"me\s+interesa|tienen|hay|m[uú]estrame|mu[eé]strame|ens[eé]name|"
    r"recomi[eé]ndame|podr[ií]an|me\s+pueden|dame|prefiero|"
    r"qu[eé]\s+m[aá]s\s+tienen)\b.{0,75}\b(?:otra\s+propiedad|otro\s+departamento|"
    r"otros?\s+(?:departamentos|deptos|inmuebles)|otras?\s+(?:propiedades|casas|"
    r"opciones|alternativas|comunas)|m[aá]s\s+(?:propiedades|casas|departamentos|"
    r"opciones|alternativas)|otra\s+comuna|comunas\s+distintas)\b|"
    r"\b(?:busco|estoy\s+buscando|quiero\s+buscar|quisiera\s+buscar|"
    r"me\s+gustar[ií]a\s+(?:buscar|ver|algo)|necesito\s+buscar|"
    r"tienen|hay|me\s+pueden\s+mostrar|podr[ií]an\s+mostrarme)\b.{0,55}\b"
    r"(?:en|por)\s+(?:otra|otras|distinta|diferentes?)\s+comunas?\b|"
    r"\b(?:busco|estoy\s+buscando|quiero\s+buscar|quisiera\s+algo|"
    r"me\s+gustar[ií]a\s+algo|tienen|hay|me\s+pueden\s+mostrar|"
    r"podr[ií]an\s+mostrarme)\b.{0,55}\b(?:algo\s+)?(?:en|por)\s+[a-z]{3,}\b|"
    r"\by\s+(?:en|por)\s+(?:la\s+)?(?:comuna\s+de\s+)?[a-z]{3,}\s*$|"
    r"\b(?:otras\s+opciones|otras\s+propiedades|otros\s+departamentos)\b"
    r"(?=\s*(?:\?|$))",
    re.IGNORECASE,
)

_DECLINE = re.compile(
    r"\b(?:no\s+me\s+interesa|no\s+estoy\s+interesad[oa]|ya\s+no\s+me\s+interesa|"
    r"no\s+gracias|no\s+me\s+conviene|descarto\s+(?:el|la)?\s*"
    r"(?:departamento|propiedad|casa|vivienda)|no\s+quiero\s+"
    r"(?:esta|esa|la)\s+(?:propiedad|casa|vivienda|departamento|depto)|"
    r"no\s+la\s+quiero|no\s+me\s+gusta\s+(?:esta|la)\s+"
    r"(?:propiedad|casa|vivienda|departamento|depto))\b",
    re.IGNORECASE,
)
_OFFER = re.compile(
    r"\b(?:quiero|quisiera|me\s+gustar[ií]a|voy\s+a|puedo|podr[ií]a|"
    r"me\s+interesa|propongo|ofrezco|ofertar[ií]a|mi\s+oferta\s+ser[ií]a)\b"
    r".{0,55}\b(?:hacer|presentar|enviar|dejar|ofrecer|ofertar|pagar|"
    r"proponer|oferta\s+por|ofrezco\s+\d|ofertar\s+\d)\b|"
    r"\b(?:ofrezco|puedo\s+ofrecer|podr[ií]a\s+ofrecer|propongo\s+pagar|"
    r"pagar[ií]a|mi\s+oferta\s+(?:es|ser[ií]a))\b.{0,35}\b"
    r"\d(?:[ .]\d{3})+(?:[,.]\d+)?|\d{3,}(?:[,.]\d+)?\b|"
    r"\b(?:te\s+)?(?:ofrezco|oferto)\b.{0,30}\b"
    r"\d(?:[ .]\d{3})+(?:[,.]\d+)?|\d{3,}(?:[,.]\d+)?\b|"
    r"\b(?:quiero|quisiera|me\s+gustar[ií]a)\s+(?:hacer|presentar|enviar)\s+"
    r"(?:una\s+)?oferta\b|\bhagamos\s+una\s+oferta\b",
    re.IGNORECASE,
)
_VISIT = re.compile(
    r"\b(?:quiero|quisiera|prefiero|me\s+gustar[ií]a|me\s+encantar[ií]a|me\s+interesa|"
    r"necesito|puedo|podr[ií]a|me\s+acomoda)\s+(?:ir\s+a\s+)?"
    r"(?:visitar\w*|ver(?:la|lo)|ver\s+(?:la|el|esta|esa)\s+"
    r"(?:propiedad|casa|vivienda|departamento|depto)|"
    r"conocer(?:la|lo)|conocer\s+(?:la|el|esta|esa)\s+"
    r"(?:propiedad|casa|vivienda|departamento|depto))\b|"
    r"\b(?:quiero|quisiera|me\s+gustar[ií]a|necesito|podemos|podr[ií]amos)\s+"
    r"(?:agendar|coordinar|programar)\s+(?:una\s+)?visita\b|"
    r"\b(?:agendemos|coordinemos|programemos)\b.{0,35}\bvisita\b|"
    r"\b(?:se\s+puede|es\s+posible|hay\s+disponibilidad\s+para)\s+"
    r"(?:visitar|ver(?:la|lo)|ver\s+(?:la|el|esta|esa)\s+"
    r"(?:propiedad|casa|vivienda|departamento|depto)|"
    r"conocer(?:la|lo)|conocer\s+(?:la|el|esta|esa)\s+"
    r"(?:propiedad|casa|vivienda|departamento|depto))\b|"
    r"\b(?:tienen|hay)\s+(?:disponibilidad|horarios?|horas?)\s+(?:para\s+)?"
    r"(?:visitar|ver(?:la|lo)?|conocer(?:la|lo)?|una\s+visita)\b|"
    r"\b(?:cu[aá]ndo|qu[eé]\s+d[ií]a|a\s+qu[eé]\s+hora)\s+(?:puedo|podr[ií]a)\s+"
    r"(?:ir\s+a\s+ver\s+(?:(?:la|el|esta|esa)\s+)?"
    r"(?:propiedad|casa|vivienda|departamento|depto)|visitar|"
    r"conocer(?:la|lo)|conocer\s+(?:la|el|esta|esa)\s+"
    r"(?:propiedad|casa|vivienda|departamento|depto))\b|"
    r"\b(?:visita|visitar)\s+(?:hoy|ahora|ma[nñ]ana|pasado\s+ma[nñ]ana|"
    r"el\s+(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo))\b|"
    r"\b(?:quiero|quisiera|me\s+gustar[ií]a)\s+(?:una\s+)?visita\b",
    re.IGNORECASE,
)
_CONTACT = re.compile(
    r"\b(?:quiero|quisiera|necesito|me\s+gustar[ií]a|solicito|podr[ií]a|"
    r"pueden|puede|me\s+pueden|me\s+podr[ií]an|por\s+favor)\b.{0,45}\b"
    r"(?:que\s+me\s+llam\w*|que\s+me\s+contact\w*|que\s+me\s+escrib\w*|"
    r"hablar\s+con\s+(?:un\s+|una\s+)?(?:ejecutiv\w*|asesor\w*|corredor\w*)|"
    r"(?:una\s+)?llamada|contacto\s+telef[oó]nico)\b|"
    r"\b(?:ll[aá]mame|ll[aá]menme|contactenme|me\s+pueden\s+llamar|"
    r"me\s+puedes\s+llamar|me\s+podr[ií]as\s+llamar|puedes\s+llamarme|"
    r"me\s+llamas|me\s+podr[ií]an\s+llamar|pueden\s+llamarme|"
    r"que\s+me\s+contacte\s+(?:un\s+|una\s+)?"
    r"(?:ejecutiv\w*|asesor\w*|corredor\w*))\b",
    re.IGNORECASE,
)
_INTEREST = re.compile(
    r"\b(?:me\s+interesa|estoy\s+interesad[oa]|s[ií]\s+me\s+interesa|"
    r"lo\s+quiero\s+considerar|me\s+parece\s+interesante)\b",
    re.IGNORECASE,
)
_INFORMATION = re.compile(
    r"\b(?:cu[aá]nto\s+(?:cuesta|vale|son|baj[oó])|gastos?\s+comunes|"
    r"(?:los\s+)?gc\b|qu[eé]\s+(?:gastos|caracter[ií]sticas|incluye|orientaci[oó]n|"
    r"superficie|comuna|documentos)|cu[aá]les\s+(?:son|tiene)|tiene\s+"
    r"(?:bodega|estacionamiento|ascensor|terraza)|es\s+(?:disponible|negociable)|"
    r"hay\s+(?:estacionamiento|gastos\s+comunes)|informaci[oó]n|detalle|"
    r"precio|hipotecario|contribuciones|orientaci[oó]n|metros\s+cuadrados)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CampaignClassification:
    label: str
    campaign_priority: str | None
    intent: str
    reason: str
    positive: bool
    needs_human_review: bool
    explicit_visit: bool = False
    explicit_contact: bool = False
    explicit_offer: bool = False
    alternatives_requested: bool = False
    do_not_call: bool = False
    visit_declined: bool = False

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "campaign_priority": self.campaign_priority,
            "intent": self.intent,
            "reason": self.reason,
            "positive": self.positive,
            "needs_human_review": self.needs_human_review,
            "visit_requested": self.explicit_visit,
            "human_requested": self.explicit_contact,
            "offer_intent": self.explicit_offer,
            "alternatives_requested": self.alternatives_requested,
            "do_not_call": self.do_not_call,
            "visit_declined": self.visit_declined,
            "contact_preference": "DO_NOT_CALL" if self.do_not_call else None,
            "visit_preference": "DECLINED" if self.visit_declined else None,
            "classifier_version": CLASSIFIER_VERSION,
        }


def has_do_not_call_preference(text: str) -> bool:
    """True when a clause declines calls; does not imply a global opt-out."""
    return any(_NEGATED_CALL.search(clause) for clause in _clauses(text))


def has_declined_visit_preference(text: str) -> bool:
    """True when the customer declines a visit/action, not necessarily the home."""
    return any(_NEGATED_VISIT.search(clause) for clause in _clauses(text))


def has_alternative_request(text: str) -> bool:
    """Return only affirmative searches for other properties or communes."""
    return any(
        _ALTERNATIVES.search(clause) and not _NEGATED_ALTERNATIVE.search(clause)
        for clause in _clauses(text)
    )


def _is_global_opt_out(raw: str) -> bool:
    message = _plain(raw)
    if _OPT_OUT_ONLY.fullmatch(message):
        return True
    # Evaluate each clause independently so "no quiero llamadas, pero sí
    # mensajes" cannot be mistaken for a global opt-out.
    return any(_STOP.search(clause) for clause in _clauses(raw))


def _is_completed_search(message: str, raw: str) -> bool:
    if _HYPOTHETICAL_FOUND.search(_fold(raw)):
        return False
    return any(
        _ALREADY_FOUND.search(clause) and not _NEGATED_FOUND.search(clause)
        for clause in (_clauses(raw) or [message])
    )


def _property_rejection(clauses: list[str], *, visit_declined: bool) -> bool:
    for clause in clauses:
        if not _DECLINE.search(clause):
            continue
        # Declining alternatives or another commune is not a rejection of the
        # campaign's property.
        if _NEGATED_ALTERNATIVE.search(clause):
            continue
        if visit_declined and _NEGATED_VISIT.search(clause):
            continue
        if _NEGATED_CALL.search(clause):
            continue
        # "No me interesa visitar" is a visit preference. It is never a
        # property rejection, even when there is no second clause.
        if _NEGATED_VISIT.search(clause):
            continue
        return True
    return False


def classify_price_drop_reply(text: str) -> CampaignClassification:
    raw = str(text or "")
    message = _plain(raw)
    if not message:
        return CampaignClassification(NO_SIGNAL, None, "OTHER", "empty_or_unreadable", False, False)
    if _is_global_opt_out(raw):
        return CampaignClassification(UNSUBSCRIBED, None, "UNSUBSCRIBE", "explicit_opt_out", False, False)
    if _is_completed_search(message, raw):
        return CampaignClassification(UNSUBSCRIBED, None, "UNSUBSCRIBE", "already_found_property", False, False)

    clauses = _clauses(raw) or [message]
    do_not_call = any(_NEGATED_CALL.search(clause) for clause in clauses)
    visit_declined = any(_NEGATED_VISIT.search(clause) for clause in clauses)
    explicit_offer = any(
        _OFFER.search(clause) and not _NEGATED_OFFER.search(clause)
        for clause in clauses
    )
    explicit_visit = not visit_declined and any(
        _VISIT.search(clause) for clause in clauses
    )
    explicit_contact = not do_not_call and any(
        _CONTACT.search(clause) and not _NEGATED_CALL.search(clause)
        for clause in clauses
    )
    alternatives = has_alternative_request(raw)
    interest = any(
        _INTEREST.search(clause) and not _NEGATED_INTEREST.search(clause)
        for clause in clauses
    )
    information = any(_INFORMATION.search(clause) for clause in clauses) or "?" in raw
    rejection = _property_rejection(clauses, visit_declined=visit_declined)

    if explicit_offer:
        result = CampaignClassification(
            PRIORITY, "ALTA", "GIVE_OFFER", "explicit_offer", True, True,
            explicit_offer=True, do_not_call=do_not_call,
            visit_declined=visit_declined,
        )
    elif explicit_visit:
        result = CampaignClassification(
            PRIORITY, "ALTA", "ASK_VISIT", "explicit_visit_request", True, True,
            explicit_visit=True, do_not_call=do_not_call,
            visit_declined=visit_declined,
        )
    elif explicit_contact:
        result = CampaignClassification(
            PRIORITY, "ALTA", "ASK_CONTACT", "explicit_human_contact_request", True, True,
            explicit_contact=True, do_not_call=do_not_call,
            visit_declined=visit_declined,
        )
    elif alternatives:
        result = CampaignClassification(
            ALTERNATIVES, "MEDIA", "ASK_ALTERNATIVES", "other_property_search", True, True,
            alternatives_requested=True, do_not_call=do_not_call,
            visit_declined=visit_declined,
        )
    elif interest:
        result = CampaignClassification(
            INTEREST, "MEDIA", "ASK_INFO", "explicit_general_interest", True, True,
            do_not_call=do_not_call, visit_declined=visit_declined,
        )
    elif information or visit_declined or do_not_call:
        reason = (
            "visit_preference_and_information" if visit_declined and information else
            "visit_preference_only" if visit_declined else
            "contact_preference_only" if do_not_call else
            "information_question"
        )
        result = CampaignClassification(
            INFO, "INFORMATIVA", "ASK_INFO", reason, False, False,
            do_not_call=do_not_call, visit_declined=visit_declined,
        )
    elif rejection:
        result = CampaignClassification(
            DECLINED, None, "OTHER", "explicit_rejection", False, False,
            do_not_call=do_not_call, visit_declined=visit_declined,
        )
    else:
        result = CampaignClassification(
            NO_SIGNAL, None, "OTHER", "insufficient_intent", False, False,
            do_not_call=do_not_call, visit_declined=visit_declined,
        )

    if result.label == PRIORITY and result.intent not in HOT_INTENTS:
        raise RuntimeError("campaign_high_intent_not_canonical")
    return result


def lead_must_not_be_contacted(lead: dict | None) -> bool:
    """Read-only fail-closed guard for recorded opt-outs and completed searches.

    Existing customer evidence prevents future sends even when an older
    inbound message was misclassified. A human-controlled re-opt-in must clear
    the suppression explicitly.
    """
    if not isinstance(lead, dict):
        return False
    if lead.get("conversation_status") in {
        "STOPPED_BY_CLIENT", "STOPPED", "BLOCKED", "BLOCKED_EXTERNAL_BROKER",
    }:
        return True
    for key in ("price_drop_v2", "price_drop_campaign_v1"):
        if (lead.get(key) or {}).get("opted_out") is True:
            return True
    for campaign in ((lead.get("price_drop_v2") or {}).get("campaigns") or []):
        if not isinstance(campaign, dict):
            continue
        if campaign.get("status") == "OPTED_OUT":
            return True
        if classify_price_drop_reply(campaign.get("last_customer_text") or "").label == UNSUBSCRIBED:
            return True
    for msg in lead.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") in ("user", "customer"):
            if classify_price_drop_reply(msg.get("content") or "").label == UNSUBSCRIBED:
                return True
    return False
