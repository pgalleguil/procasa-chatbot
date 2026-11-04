"""V10 isolated executive WhatsApp notice, always addressed to the test phone.

No CRM, leads, assignments, real executive phone or production campaign collection
is ever read or written here. One simulation after a clearly positive property
response in the V10 conversation, never for V9 or live leads.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

from pymongo.errors import DuplicateKeyError

from .phone_utils import normalize_phone_strict

VERSION = "price_drop_pilot_v10_exec_notice_test"
FLAG = "PRICE_DROP_PILOT_EXEC_NOTICE_SIMULATION"
TURNS = "crm_price_drop_pilot_test_turns_v1"

_YES = re.compile(
    r"^\s*(?:s[ií]|s[ií]\s*,?\s*por favor|claro(?:\s+que\s+s[ií])?|"
    r"por supuesto|ya\s*,?\s*s[ií]|dale\s*,?\s*s[ií])\s*[!.]?\s*$", re.I
)
_RELEVANT = re.compile(
    r"(?:interes|visita|visitar|agendar|coordinar|verlo|verla|"
    r"avanzar|retomar|retomemos|contact|llama|precio|rebaja|"
    r"nuevo valor|cómo lo ves|como lo ves|día|dia|semana|hora|horario)", re.I
)
_QUESTION = re.compile(r"(?:\?|¿)")


def should_simulate(message: str, history: list[dict]) -> bool:
    """Accept explicit interest in natural language, or contextual yes.

    This test-only detector reuses the campaign's own classifications, never
    the lead HOT/NORMAL states from CRM. It cannot inspect lead records.
    """
    from .price_drop_campaign_classifier import (
        INTEREST, PRIORITY, classify_price_drop_reply,
    )
    value = str(message or "").strip()
    classification = classify_price_drop_reply(value)
    if classification.label in {INTEREST, PRIORITY} and classification.positive:
        return True
    if not _YES.fullmatch(value):
        return False
    for turn in reversed((history or [])[-3:]):
        previous = str(turn.get("bot_text") or "").strip()
        if previous:
            return bool(_QUESTION.search(previous) and _RELEVANT.search(previous))
    return False


def _notice_text(ctx: dict, phone: str, customer_text: str) -> str:
    code = str(ctx.get("property_code") or "")
    old = int(ctx.get("old_price_uf") or 0)
    new = int(ctx.get("new_price_uf") or 0)
    if not code.isdigit() or not old or not new or new >= old:
        raise ValueError("verified_campaign_prices_required")
    formatted = lambda number: f"{number:,.0f}".replace(",", ".")
    # The original text is user-authored test content; no property/CRM private
    # fields are included in this notice. Shorten and normalize whitespace.
    quoted = " ".join(str(customer_text or "").split())[:120]
    from .price_drop_campaign_classifier import PRIORITY, classify_price_drop_reply
    category = classify_price_drop_reply(customer_text).label
    label = ("Alta intención" if category == PRIORITY else "Interesado")
    return (
        "🧪 *PRUEBA INTERNA — AVISO SIMULADO AL EJECUTIVO*\n\n"
        "🔔 *PROCASA — Respuesta a campaña de rebaja de precio*\n\n"
        f"🏠 Propiedad: {code}\n"
        f"📉 Rebaja informada: UF {formatted(old)} → UF {formatted(new)}\n"
        f"*Estado campaña:* Rebaja de precio – {label}\n"
        f"📱 *Cliente de prueba:* ***{str(phone)[-4:]}\n"
        f"*Respuesta detectada:* «{quoted}»\n"
        "*Motivo:* Manifestó interés después del aviso de rebaja.\n\n"
        "👤 *Destinatario:* ejecutivo del último ciclo de asignación vigente "
        "(simulado; no se consultaron datos de un lead real).\n\n"
        "🔗 *Abrir lead en CRM:* no disponible en esta prueba aislada. "
        "En un envío real se incluye la URL autenticada del lead existente.\n\n"
        "Revisar el lead en CRM para gestionar su interés. "
        "*No es un lead nuevo ni una reasignación.*\n\n"
        "⚠️ Solo prueba: este aviso se envió al teléfono de prueba, "
        "no a un ejecutivo ni a un cliente real."
    )


def send_simulated_exec_notice(
    db: Any, *, ctx: dict, phone: str, customer_text: str,
    history: list[dict], inbound_message_id: str,
    sender: Callable[[str, str], dict] | None = None,
) -> dict:
    if os.getenv(FLAG, "").lower() not in {"1", "true", "yes"}:
        return {"status": "DISABLED"}
    if ctx.get("campaign_version") != VERSION:
        return {"status": "WRONG_VERSION"}
    from .price_drop_pilot_isolation import is_test_phone
    normalized = normalize_phone_strict(phone)
    if not normalized or not is_test_phone(normalized):
        return {"status": "OUTSIDE_TEST_ALLOWLIST"}
    if not should_simulate(customer_text, history):
        return {"status": "NO_CONFIRMED_INTEREST"}
    key = "exec-sim:" + hashlib.sha256(
        str(ctx["campaign_id"]).encode("utf-8")
    ).hexdigest()
    now = datetime.now(timezone.utc)
    # Insert before contacting WhatsApp. Never reattempt an uncertain delivery.
    try:
        db[TURNS].insert_one({
            "_id": key,
            "campaign_id": ctx["campaign_id"],
            "phone_last4": normalized[-4:],
            "provider_inbound_id": str(inbound_message_id),
            "status": "EXEC_NOTICE_SENDING",
            "test_exec_notice": True,
            "created_at": now,
        })
    except DuplicateKeyError:
        return {"status": "ALREADY_ATTEMPTED"}
    msg = _notice_text(ctx, normalized, customer_text)
    if sender is None:
        from .whatsapp_client import send_whatsapp_message_detailed_sync
        sender = send_whatsapp_message_detailed_sync
    try:
        receipt = sender(normalized, msg)
    except Exception:
        db[TURNS].update_one({"_id": key}, {"$set": {
            "status": "EXEC_NOTICE_DELIVERY_UNKNOWN",
            "attempted_at": now,
        }})
        return {"status": "DELIVERY_UNKNOWN"}
    accepted = bool(
        receipt.get("success") and receipt.get("provider_message_id")
        and not receipt.get("provider_call_uncertain")
    )
    sent_at = datetime.now(timezone.utc)
    db[TURNS].update_one({"_id": key}, {"$set": {
        "status": "EXEC_NOTICE_SENT" if accepted else "EXEC_NOTICE_DELIVERY_UNKNOWN",
        "provider_outbound_id": receipt.get("provider_message_id"),
        "provider_delivery_status": receipt.get("delivery_status"),
        "attempted_at": now,
        **({"sent_at": sent_at} if accepted else {}),
    }})
    return {
        "status": "SENT" if accepted else "DELIVERY_UNKNOWN",
        "phone_last4": normalized[-4:],
        "campaign_id": ctx["campaign_id"],
        "provider_message_id": receipt.get("provider_message_id"),
    }


def recover_v10_exec_notice_once(
    db: Any, *, sender: Callable[[str, str], dict] | None = None,
) -> dict:
    """Deliver one missed simulated alert for an ALREADY-SENT V10 conversation.

    Operates on whitelisted test data only. No production lead, CRM,
    assignment, executive phone or real-campaign collection can be accessed.
    An exact campaign _id is used and the unique notice ID is reserved
    before the provider call, so service restarts do not resend.
    """
    if os.getenv(FLAG, "").lower() not in {"1", "true", "yes"}:
        return {"status": "DISABLED"}
    from .price_drop_pilot_isolation import is_test_phone, load_test_context
    from .price_drop_pilot import COLLECTION as TEST_CAMPAIGNS
    phone = normalize_phone_strict(os.getenv("PRICE_DROP_PILOT_TEST_PHONE", ""))
    if not phone or not is_test_phone(phone):
        return {"status": "NOT_WHITELISTED"}
    now = datetime.now(timezone.utc)
    campaign = db[TEST_CAMPAIGNS].find_one({
        "phone_normalized": phone,
        "test_mode": True,
        "campaign_version": VERSION,
        "lead_id": {"$exists": False},
        "status": "SENT",
        "expires_at": {"$gt": now},
    }, {"_id": 1, "phone_normalized": 1, "campaign_version": 1},
       sort=[("sent_at", -1)])
    if not campaign:
        return {"status": "NO_ACTIVE_V10"}
    ctx = load_test_context(db, phone)
    if not ctx or ctx.get("campaign_id") != campaign["_id"] or ctx.get("campaign_version") != VERSION:
        return {"status": "CONTEXT_MISMATCH"}
    # Read ONLY turns from this exact isolated test campaign. Limit scanning,
    # and never use the lead, CRM or production messaging collections.
    turns = list(db[TURNS].find({
        "campaign_id": campaign["_id"],
        "status": "SENT",
    }, {"customer_text": 1, "bot_text": 1, "provider_inbound_id": 1,
        "created_at": 1}).sort("created_at", 1).limit(240))
    previous = []
    for turn in turns:
        text = str(turn.get("customer_text") or "")
        if should_simulate(text, previous):
            outcome = send_simulated_exec_notice(
                db, ctx=ctx, phone=phone, customer_text=text,
                history=previous, inbound_message_id=str(
                    turn.get("provider_inbound_id") or turn.get("_id") or "historical"
                ),
                sender=sender,
            )
            return {
                "status": outcome["status"],
                "phone_last4": phone[-4:],
                "campaign_id": campaign["_id"],
                "reason": "recovered_positive_v10_reply",
                "provider_message_id": outcome.get("provider_message_id"),
            }
        previous.append(turn)
    return {"status": "NO_CONFIRMED_INTEREST"}
