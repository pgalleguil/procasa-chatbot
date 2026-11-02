"""Side-effect-free rehearsals of commercial replies to a price-drop message.

Never imports CRM writers or the WhatsApp sender. These are hypothetical
routing decisions, not completed visits, reservations or notifications.
"""
from __future__ import annotations

import re

OPT_OUT = re.compile(r"\b(?:no me escriban|no me contacten|no quiero (?:m[aá]s )?mensajes|darme de baja|stop)\b", re.I)
DECLINE = re.compile(r"^\s*(?:no,?\s*gracias|no me interesa|ya no me interesa|por ahora no)\b", re.I)
NO_VISIT = re.compile(r"\b(?:no quiero visitar|no quiero una visita|no me interesa visitar)\b", re.I)
VISIT = re.compile(r"\b(?:visitar|visita|agendar|coordinar una visita|ir a (?:verlo|verla|conocerlo|conocerla))\b", re.I)
OFFER = re.compile(r"\b(?:oferta|ofertar|negociar|contraoferta|aceptan menos|precio negociable)\b", re.I)
HUMAN = re.compile(r"\b(?:ejecutiv[oa]|asesor[oa]|que me llamen|pueden llamarme|hablar con alguien)\b", re.I)
INTEREST = re.compile(r"^\s*(?:s[ií]|me interesa|claro|dale|cu[eé]ntame|perfecto|ok(?:a|ey)?)\b", re.I)
INFO = re.compile(r"\b(?:ba[ñn]os?|dormitorios?|precio|gastos? comunes?|metros|ubicaci[oó]n|estacionamiento)\b", re.I)


def shadow_reply_decision(candidate: dict, text: str) -> dict:
    """Compute a hypothetical handoff; do not perform any external action."""
    if (not isinstance(candidate, dict) or
        candidate.get("decision") != "HOLD_FOR_MANUAL_REVIEW" or
        not candidate.get("current_owner") or
        candidate.get("marketing_consent") != "UNVERIFIED"):
        return {"status": "BLOCKED_INVALID_PREVIEW", "crm_written": False,
                "whatsapp_sent": False, "executive_notified": False}
    clean = str(text or "").strip()[:1600]
    if OPT_OUT.search(clean):
        intent, next_step = "OPT_OUT", "SUPPRESS_FUTURE_CONTACT_REQUIRES_LIVE_INTEGRATION"
    elif DECLINE.search(clean):
        intent, next_step = "DECLINE", "CLOSE_CAMPAIGN_INTEREST_REQUIRES_LIVE_INTEGRATION"
    elif NO_VISIT.search(clean):
        intent, next_step = "NO_VISIT", "CONTINUE_INFORMATION_ONLY"
    elif VISIT.search(clean):
        intent, next_step = "VISIT_REQUEST", "ROUTE_REQUEST_TO_EXISTING_OWNER_AFTER_APPROVAL"
    elif OFFER.search(clean):
        intent, next_step = "OFFER_REQUEST", "ROUTE_REQUEST_TO_EXISTING_OWNER_AFTER_APPROVAL"
    elif HUMAN.search(clean):
        intent, next_step = "HUMAN_REQUEST", "ROUTE_REQUEST_TO_EXISTING_OWNER_AFTER_APPROVAL"
    elif INTEREST.search(clean):
        intent, next_step = "INTEREST", "CONFIRM_NEEDS_BEFORE_HANDOFF"
    elif INFO.search(clean):
        intent, next_step = "ASK_INFO", "ANSWER_VERIFIED_PROPERTY_FACTS"
    else:
        intent, next_step = "UNRESOLVED", "ASK_CLARIFYING_QUESTION"
    return {
        "status": "SHADOW_ONLY",
        "customer_intent": intent,
        "proposed_next_step": next_step,
        "existing_owner": candidate["current_owner"],
        "property_code": candidate["property_code"],
        "crm_written": False,
        "whatsapp_sent": False,
        "executive_notified": False,
        "visit_booked": False,
        "requires_live_handoff_implementation": intent in {
            "VISIT_REQUEST", "OFFER_REQUEST", "HUMAN_REQUEST", "OPT_OUT", "DECLINE",
        },
    }
