"""Campaign-only commercial response classifier.

Aligns HOT with the existing canonical lead_temperature.HOT_INTENTS,
without mutating the canonical CRM lead temperature, owners or SLA cycles.
All decisions are explicit, deterministic and explainable. Unknown/ambiguous
phrases are not promoted to HOT or counted as a positive opportunity.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .lead_temperature import HOT, HOT_INTENTS

CLASSIFIER_VERSION = "price_drop_response_rules_v1"
NORMAL = "NORMAL"
NO_SIGNAL = "SIN_SENAL"
DECLINED = "RECHAZADO"
UNSUBSCRIBED = "BAJA"


def _plain(text: str) -> str:
    deaccented = "".join(
        ch for ch in unicodedata.normalize("NFKD", str(text or "").lower())
        if not unicodedata.combining(ch)
    )
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", deaccented)).strip()


_STOP = re.compile(
    r"\b(?:stop|darme de baja|quiero la baja|elimin(?:a|en) mi numero|"
    r"no me (?:escriban|contacten|manden (?:mas )?mensajes)|"
    r"no quiero (?:mas )?mensajes|dejen de escribir(?:me)?)\b",
)
_DECLINE = re.compile(
    r"\b(?:no me interesa|no estoy interesado|no estoy interesada|"
    r"ya no me interesa|no gracias|no me conviene|"
    r"descarto (?:el|la) (?:departamento|propiedad)|"
    r"no quiero (?:agendar|visitar|una visita|ir a ver|comprar|verlo|verla))\b",
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
    r"me interesa|quiero coordinar)\b.{0,50}"
    r"\b(?:visitar|visita|verlo|verla|ir a ver|conocerlo|conocerla|agendar)\b"
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
    temperature: str | None
    intent: str
    reason: str
    positive: bool
    needs_human_review: bool
    explicit_visit: bool = False
    explicit_contact: bool = False
    explicit_offer: bool = False

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "temperature": self.temperature,
            "intent": self.intent,
            "reason": self.reason,
            "positive": self.positive,
            "needs_human_review": self.needs_human_review,
            "visit_requested": self.explicit_visit,
            "human_requested": self.explicit_contact,
            "offer_intent": self.explicit_offer,
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
    if _DECLINE.search(message):
        return CampaignClassification(
            DECLINED, None, "OTHER", "explicit_rejection", False, False,
        )

    if _OFFER.search(message):
        intent, reason = "GIVE_OFFER", "explicit_offer"
        result = CampaignClassification(
            HOT, HOT, intent, reason, True, True, explicit_offer=True,
        )
    elif _VISIT.search(message):
        intent, reason = "ASK_VISIT", "explicit_visit_request"
        result = CampaignClassification(
            HOT, HOT, intent, reason, True, True, explicit_visit=True,
        )
    elif _CONTACT.search(message):
        intent, reason = "ASK_CONTACT", "explicit_human_contact_request"
        result = CampaignClassification(
            HOT, HOT, intent, reason, True, True, explicit_contact=True,
        )
    elif _INTEREST.search(message):
        result = CampaignClassification(
            NORMAL, NORMAL, "ASK_INFO", "explicit_general_interest", True, True,
        )
    elif "?" in str(text or "") or _INFORMATION.search(message):
        result = CampaignClassification(
            NORMAL, NORMAL, "ASK_INFO", "information_question", False, False,
        )
    else:
        result = CampaignClassification(
            NO_SIGNAL, None, "OTHER", "insufficient_intent", False, False,
        )

    # Never diverge silently from the canonical CRM HOT intent definition.
    if result.temperature == HOT and result.intent not in HOT_INTENTS:
        raise RuntimeError("campaign_hot_intent_not_canonical")
    return result
