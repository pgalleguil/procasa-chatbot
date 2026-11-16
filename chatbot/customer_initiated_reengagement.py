"""Allow explicit customer-initiated service requests without restoring marketing consent.

An historical campaign STOP or completed search must continue to exclude the
person from all unsolicited price-drop campaigns. A fresh inbound request to
buy, rent, sell, or obtain brokerage assistance is a separate conversation.
"""
from __future__ import annotations

import re
import unicodedata

from .price_drop_campaign_classifier import classify_price_drop_reply

_SERVICE_REQUEST = re.compile(
    r"\b(?:"
    r"(?:quiero|quisiera|necesito|busco|buscando|estoy buscando|"
    r"me gustaria|me interesa|podrian|podrias|pueden|puedes|"
    r"me pueden|me podrian|quiero que|necesito que)"
    r"\b.{0,90}\b"
    r"(?:comprar|comprarme|arrendar|arriendo|alquilar|alquiler|"
    r"vender|vendan|vendiend[oa]|venta|buscar|encontrar|"
    r"propiedad|propiedades|departamento|depto|casa|local|"
    r"bodega|terreno|oficina|inmueble|corredor|corredora|"
    r"agendar visita|visitar|cotizar|asesoria|asesoramiento|"
    r"ayud\w*|asesor\w*|orient\w*|tasacion|tasar|administrar|administren|publicar|publiquen|arrienden)"
    r"|(?:tengo|poseo)\s+(?:(?:una?|mi)\s+)?"
    r"(?:propiedad|casa|depto|departamento|terreno|local|inmueble)"
    r"\b.{0,65}\b(?:vender|arrendar|publicar|tasar|tasacion|administrar)"
    r"|(?:me|nos)\s+(?:puedes?|podrias|podrian|pueden)\s+"
    r"(?:ayud\w*|asesor\w*|orient\w*)\b"
    r"|(?:ayudame|asesorenme|asesorame)\b"
    r"|(?:tienen|hay)\b.{0,65}\b"
    r"(?:propiedades|departamentos|deptos|casas|terrenos|inmuebles|"
    r"para vender|en arriendo|a la venta)"
    r")\b",
    re.IGNORECASE,
)
_NEGATED_NEW_REQUEST = re.compile(
    r"\b(?:no\s+(?:quiero|necesito|deseo|busco|estoy buscando|"
    r"me interesa|me interesa buscar)|ya\s+no\s+(?:quiero|necesito|busco))\b",
    re.IGNORECASE,
)


def _fold(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", str(value or "").casefold())
        if not unicodedata.combining(ch)
    )


def customer_initiated_service_request_allowed(lead: dict | None, message: str) -> bool:
    """One-turn inbound exception; never clears STOP, blocked marketing or consent.

    The decision is deliberately strict: a mere greeting, thanks, newsletter
    question, historical message replay, or explicit opt-out does not authorize
    a chatbot response. Only the new inbound request itself can grant it.
    """
    if not isinstance(lead, dict):
        return False
    if lead.get("conversation_status") in {"BLOCKED", "BLOCKED_EXTERNAL_BROKER"}:
        return False
    text = str(message or "").strip()
    if not text or len(text) > 8000:
        return False
    classification = classify_price_drop_reply(text)
    # "Tengo un departamento que quiero vender" contains ownership evidence,
    # which the campaign classifier can interpret as completed search. A
    # genuine *new* brokerage request still deserves an answer. Never
    # override an explicit request to stop contacting the customer.
    if classification.label == "BAJA" and classification.reason != "already_found_property":
        return False
    folded = _fold(text)
    if _NEGATED_NEW_REQUEST.search(folded):
        return False
    # Fresh customer requests remain answerable even if a prior reply said
    # STOP or "already found" and the lead is suppressed for marketing.
    return bool(_SERVICE_REQUEST.search(folded))
