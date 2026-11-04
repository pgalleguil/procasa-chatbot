"""Two separate, test-only WhatsApp notices to the configured tester.

This module never reads CRM leads, executive contacts or assignment history.
"""
import hashlib
import os
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from .phone_utils import normalize_phone_strict
from .price_drop_pilot_exec_notice_sim import FLAG, TURNS, should_simulate

VERSION = "price_drop_pilot_v11_dual_notice_test"
V12_VERSION = "price_drop_pilot_v12_followup_test"


def _text(ctx, phone, answer, role):
    old = int(ctx["old_price_uf"])
    new = int(ctx["new_price_uf"])
    if ctx["property_code"] != "6436" or old != 2790 or new != 2690:
        raise ValueError("listing_not_verified")
    state = "Alta intención" if any(
        word in answer.casefold() for word in ("visita", "visitar", "llame", "oferta")
    ) else "Interesado"
    header = ("ADMINISTRADOR" if role == "ADMIN" else "EJECUTIVO")
    action = ("Revisar campaña y seguimiento." if role == "ADMIN"
              else "Contactar al cliente para continuar su consulta.")
    return (
        f"🧪 *PRUEBA — AVISO AL {header}*\n"
        "🔔 *PROCASA | Rebaja de precio*\n\n"
        f"🏠 *Propiedad:* 6436 · San Miguel\n"
        f"📉 *Precio:* UF 2.790 → UF 2.690\n"
        f"📱 *Teléfono del cliente:* {phone}\n"
        f"*Estado:* Rebaja de precio – {state}\n"
        f"💬 *Respondió:* «{' '.join(answer.split())[:130]}»\n\n"
        "🔗 *CRM:* sin enlace real en esta prueba aislada.\n"
        f"📌 {action}\n"
        "⚠️ Simulación: mensaje enviado únicamente al teléfono de prueba."
    )


def send_dual_test_notices(db, *, ctx, phone, customer_text, history,
                           inbound_message_id, sender=None):
    if os.getenv(FLAG, "").lower() not in {"1", "true", "yes"}:
        return {"status": "DISABLED"}
    if ctx.get("campaign_version") not in {VERSION, V12_VERSION}:
        return {"status": "WRONG_VERSION"}
    from .price_drop_pilot_isolation import is_test_phone
    normalized = normalize_phone_strict(phone)
    configured = normalize_phone_strict(os.getenv("PRICE_DROP_PILOT_TEST_PHONE", ""))
    if not normalized or not configured or normalized != configured or not is_test_phone(normalized):
        return {"status": "OUTSIDE_TEST_ALLOWLIST"}
    if not should_simulate(customer_text, history):
        return {"status": "NO_CONFIRMED_INTEREST"}
    if sender is None:
        from .whatsapp_client import send_whatsapp_message_detailed_sync
        sender = send_whatsapp_message_detailed_sync
    outcomes = {}
    for role in ("ADMIN", "EXECUTIVE"):
        prefix = "v12-notice:" if ctx.get("campaign_version") == V12_VERSION else "v11-notice:"
        key = prefix + hashlib.sha256(
            f"{ctx['campaign_id']}:{role}".encode()
        ).hexdigest()
        now = datetime.now(timezone.utc)
        message = _text(ctx, normalized, customer_text, role)
        try:
            db[TURNS].insert_one({
                "_id": key, "campaign_id": ctx["campaign_id"],
                "status": "NOTICE_SENDING", "test_exec_notice": True,
                "test_notification_role": role, "created_at": now,
                "phone_last4": normalized[-4:],
                "provider_inbound_id": inbound_message_id,
            })
        except DuplicateKeyError:
            outcomes[role] = "ALREADY_ATTEMPTED"
            continue
        try:
            receipt = sender(normalized, message) or {}
        except Exception:
            receipt = {}
        accepted = bool(receipt.get("success") and receipt.get("provider_message_id")
                        and not receipt.get("provider_call_uncertain"))
        db[TURNS].update_one({"_id": key}, {"$set": {
            "status": "NOTICE_SENT" if accepted else "NOTICE_DELIVERY_UNKNOWN",
            "provider_outbound_id": receipt.get("provider_message_id"),
            "provider_delivery_status": receipt.get("delivery_status"),
            "attempted_at": now,
            **({"sent_at": datetime.now(timezone.utc)} if accepted else {}),
        }})
        outcomes[role] = "SENT" if accepted else "DELIVERY_UNKNOWN"
    return {"status": "PROCESSED", "roles": outcomes}
