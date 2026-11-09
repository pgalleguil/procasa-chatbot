"""Campaign-specific response intent, separate from canonical CRM temperature.

Reuses the CRM's strong-action intent semantics without using CRM HOT/NORMAL
labels, changing its lifecycle, or treating pre-existing HOT as newly reactivated.
Only replies after an accepted campaign send are classified by the caller.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .lead_temperature import HOT_INTENTS

CLASSIFIER_VERSION = "price_drop_response_rules_v3"
# Never reuse the CRM's HOT/COLD/NORMAL labels for a campaign.
PRIORITY = "PRIORIDAD_COMERCIAL"
INTEREST = "INTERES_DECLARADO"
INFO = "CONSULTA_INFORMATIVA"
NO_SIGNAL = "SIN_SENAL"
DECLINED = "RECHAZADO"
UNSUBSCRIBED = "BAJA"
ALTERNATIVES = "BUSCA_ALTERNATIVAS"


def _plain(text: str) -> str:
    deaccented = "".join(
        ch for ch in unicodedata.normalize("NFKD", str(text or "").lower())
        if not unicodedata.combining(ch)
    )
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", deaccented)).strip()


_STOP = re.compile(
    r"\b(?:stop|no me envien mas mensajes|no deseo recibir mensajes|darme de baja|quiero la baja|elimin(?:a|en) mi numero|"
    r"no me (?:escriban|contacten|manden (?:mas )?mensajes)|"
    r"no quiero (?:mas )?mensajes|dejen de escribir(?:me)?)\b",
)
# A client who already solved their housing search is not a dormant lead.
# Treat this as a marketing-suppression signal, even without the word STOP.
_ALREADY_FOUND = re.compile(
    r"\b(?:ya|finalmente|al final)\s+"
    r"(?:encontre|compre|consegui|arrende)\s+"
    r"(?:(?:un|una|otro|otra|mi|la|el)\s+)?"
    r"(?:casa|propiedad|departamento|depto|vivienda)\b"
    r"|\b(?:ya|finalmente)\s+tengo\s+(?:mi|una|un)\s+"
    r"(?:casa|propiedad|departamento|vivienda)\b",
)

_ALTERNATIVES = re.compile(
    r"\b(?:busco|estoy buscando|quisiera|quiero|me interesan|tienen|hay|"
    r"recomiendame|pueden mostrarme|podrias mostrarme)\b.{0,48}"
    r"\b(?:otr[oa]s? (?:propiedades|casas|departamentos|deptos|alternativas|opciones)|"
    r"mas (?:propiedades|casas|departamentos|deptos|alternativas|opciones)|"
    r"otras en (?:la|el) [a-z]+)\b"
    r"|\b(?:alternativas|otras opciones|otros departamentos|otras propiedades)\b",
)

_DECLINE = re.compile(
    r"\b(?:no me interesa|no estoy interesado|no estoy interesada|"
    r"ya no me interesa|no gracias|no me conviene|"
    r"descarto (?:el|la) (?:departamento|propiedad)|"
    r"no quiero (?:agendar|coordinar|visitar|visita|una visita|ir a ver|comprar|verlo|verla))\b",
)
_OFFER = re.compile(
    r"\b(?:quiero (?:hacer|presentar|enviar) (?:una )?oferta|"
    r"voy a ofertar|quiero ofertar|"
    r"ofrezco (?:\d+|uf )|"
    r"puedo ofrecer \d+|"
    r"propongo pagar \d+|"
    r"hagamos una oferta)\b",
)
_VISIT = re.compile(
    r"\b(?:quiero|quisiera|deseo|necesito|me gustaria|podemos|podria|puedo|"
    r"me interesa|estoy interesado|estoy interesada|quiero coordinar)\b.{0,50}"
    r"\b(?:visitar|visita|verlo|verla|ver el departamento|ir a ver|conocerlo|conocerla|agendar)\b"
    r"|"
    r"\b(?:agendemos|coordinemos|reservemos) (?:una )?visita\b"
    r"|"
    r"\b(?:cuando puedo (?:ir a ver|visitar|conocer)|"
    r"a que hora puedo (?:ir a ver|visitar)|"
    r"quiero ir a verlo|quiero ir a verla)\b",
)
_CONTACT = re.compile(
    r"\b(?:quiero|quisiera|necesito|me gustaria|podria|puede|pueden|"
    r"podemos|solicito)\b.{0,50}"
    r"\b(?:que me llamen|que me llame|que me contacten|"
    r"hablar con (?:un |una |el |la )?(?:ejecutivo|ejecutiva|"
    r"asesor|asesora|corredor|corredora)|"
    r"llamada|que me escriba)\b"
    r"|"
    r"\b(?:llamame|llamenme|contactenme|quiero contacto|"
    r"me puede llamar|me pueden llamar|"
    r"que me contacte (?:un |una |el |la )?(?:ejecutivo|asesor|corredor))\b",
)
_INTEREST = re.compile(
    r"\b(?:me interesa|estoy interesad[oa]|si me interesa|"
    r"me gustaria saber|quisiera saber|quiero saber|"
    r"cuentame mas|cuenteme mas|dame mas informacion|"
    r"mandame mas informacion|podrias contarme|"
    r"lo quiero considerar|me parece interesante)\b",
)
_INFORMATION = re.compile(
    r"\b(?:cuanto (?:cuesta|vale|son)|"
    r"que (?:gastos|caracteristicas|incluye|orientacion|superficie|"
    r"comuna|documentos)|"
    r"cuales (?:son|tiene)|"
    r"tiene (?:bodega|estacionamiento|ascensor|terraza)|"
    r"es (?:disponible|negociable)|"
    r"hay (?:estacionamiento|gastos comunes)|"
    r"informacion|detalle|precio|hipotecario)\b",
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
            "classifier_version": CLASSIFIER_VERSION,
        }


def classify_price_drop_reply(text: str) -> CampaignClassification:
    message = _plain(text)
    if not message:
        return CampaignClassification(
            NO_SIGNAL, None, "OTHER", "empty_or_unreadable", False, False,
        )
    if _STOP.search(message):
        return CampaignClassification(
            UNSUBSCRIBED, None, "UNSUBSCRIBE", "explicit_opt_out", False, False,
        )
    # Conditional hypotheticals are not a declaration of a completed purchase.
    hypothetical_found = ("?" in str(text or "") and bool(re.search(
        r"\b(?:si|cuando)\s+ya\s+(?:encontre|compre|consegui|arrende)\b",
        message,
    )))
    if _ALREADY_FOUND.search(message) and not hypothetical_found:
        return CampaignClassification(
            UNSUBSCRIBED, None, "UNSUBSCRIBE", "already_found_property", False, False,
        )
    if _ALTERNATIVES.search(message):
        return CampaignClassification(
            ALTERNATIVES, "MEDIA", "ASK_ALTERNATIVES",
            "other_property_search", True, True, alternatives_requested=True,
        )
    if _DECLINE.search(message):
        return CampaignClassification(
            DECLINED, None, "OTHER", "explicit_rejection", False, False,
        )
    # Negated hypothetical requests must not generate a HOT opportunity.
    if re.search(
        r"\bno (?:puedo|podemos|voy a|me gustaria|necesito|"
        r"me interesa)\b.{0,35}"
        r"\b(?:visitar|visita|verlo|verla|ir a ver|agendar|"
        r"que me llamen|llamar)\b",
        message,
    ):
        if _INTEREST.search(message) or _INFORMATION.search(message) or "?" in str(text):
            return CampaignClassification(
                INFO, "INFORMATIVA", "ASK_INFO", "negated_action_information", False, False,
            )
        return CampaignClassification(
            NO_SIGNAL, None, "OTHER", "negated_action_not_positive", False, False,
        )

    if _OFFER.search(message):
        intent, reason = "GIVE_OFFER", "explicit_offer"
        result = CampaignClassification(
            PRIORITY, "ALTA", intent, reason, True, True, explicit_offer=True,
        )
    elif _VISIT.search(message):
        intent, reason = "ASK_VISIT", "explicit_visit_request"
        result = CampaignClassification(
            PRIORITY, "ALTA", intent, reason, True, True, explicit_visit=True,
        )
    elif _CONTACT.search(message):
        intent, reason = "ASK_CONTACT", "explicit_human_contact_request"
        result = CampaignClassification(
            PRIORITY, "ALTA", intent, reason, True, True, explicit_contact=True,
        )
    elif _INTEREST.search(message):
        result = CampaignClassification(
            INTEREST, "MEDIA", "ASK_INFO", "explicit_general_interest", True, True,
        )
    elif "?" in str(text or "") or _INFORMATION.search(message):
        result = CampaignClassification(
            INFO, "INFORMATIVA", "ASK_INFO", "information_question", False, False,
        )
    else:
        result = CampaignClassification(
            NO_SIGNAL, None, "OTHER", "insufficient_intent", False, False,
        )

    # Never diverge silently from the canonical CRM HOT intent definition.
    if result.label == PRIORITY and result.intent not in HOT_INTENTS:
        raise RuntimeError("campaign_high_intent_not_canonical")
    return result


def lead_must_not_be_contacted(lead: dict | None) -> bool:
    """Read-only fail-closed guard for recorded opt-outs and prior completed searches.

    Covers historic test leads whose inbound was previously misclassified as
    SIN_SENAL. Existing evidence prevents future sends without a DB migration.
    Explicit administrator-controlled re-opt-in must clear the suppression.
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
    # Never infer refusal from model-generated texts or property descriptions.
    for msg in lead.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") in ("user", "customer"):
            if classify_price_drop_reply(msg.get("content") or "").label == UNSUBSCRIBED:
                return True
    return False
