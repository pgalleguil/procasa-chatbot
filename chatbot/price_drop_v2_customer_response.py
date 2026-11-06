"""Small, side-effect-free safeguards for V2 price-drop customer replies.

These apply only to an explicitly sent, still-active price-drop campaign.
They do not confirm visits, send alerts, or change CRM assignments.
"""
from __future__ import annotations

import re

from .conversation_policy import extract_visit_preference
from .public_property_facts import requested_public_fact_keys

_INTEREST_ONLY = re.compile(
    r"^\s*(?:(?:s[ií]|bueno|ok|okay|dale|claro)[,!. ]+)?"
    r"(?:me\s+interesa|estoy\s+interesad[oa])"
    r"(?:\s+(?:mucho|todav[ií]a|el\s+departamento|la\s+propiedad))?"
    r"\s*[!., ]*\s*$",
    re.IGNORECASE,
)


def stabilize_v2_reply(message: str, response: str, *,
                       context: dict | None = None,
                       visit_preference: str | None = None) -> str:
    """Protect short affirmative/visit replies from generic fallback text."""
    context = context or {}
    if context.get("version") != "price_drop_leads_v2":
        return response
    text = str(message or "").strip()
    code = str(context.get("property_code") or "").strip()
    if not text or not code:
        return response

    # Explicit preference can be captured by the existing conversation engine.
    # For single-turn "me interesa, mañana a las 10" support the same
    # scheduling semantics without claiming that a visit is booked.
    preference = visit_preference
    if not preference and re.search(r"\bme\s+interesa\b", text, re.I):
        preference = extract_visit_preference(text, visit_context=True)
    if preference:
        acknowledgement = (
            f"Registré tu preferencia de horario ({preference}) para conocer "
            f"la propiedad {code}. La visita todavía no está confirmada; "
            "administración debe validar la disponibilidad y coordinarla contigo."
        )
        # If the same WhatsApp turn also asks about the property, preserve
        # every verified fact: a scheduling acknowledgement must not erase it.
        asks_facts = bool(requested_public_fact_keys(text)) or bool(re.search(
            r"\b(?:ficha|informaci[oó]n|caracter[ií]sticas|detalles)\b", text, re.I,
        ))
        if asks_facts and str(response or "").strip():
            if "todavía no está confirmada" in response:
                return response
            return f"{response.strip()}\n\n{acknowledgement}"
        return acknowledgement

    # General interest is not a request to schedule an immediate visit.
    if _INTEREST_ONLY.fullmatch(text):
        return (
            f"¡Qué bueno que te siga interesando el departamento {code}! 😊 "
            "Con la rebaja ya está a UF "
            f"{int(context.get('new_price_uf', 0)):,}".replace(",", ".")
            + ". ¿Qué detalle de la propiedad te gustaría revisar?"
        )
    return response
