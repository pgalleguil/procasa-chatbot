"""Single-recipient, opt-in price-drop WhatsApp pilot sender.

Never use this for bulk sending. Dry-run by default. Run from the trusted
application environment only after enabling the isolated test feature.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatbot.phone_utils import normalize_phone_strict
from chatbot.price_drop_pilot import COLLECTION, FLAG, TEST_PHONE
from chatbot.price_drop_pilot_isolation import is_test_phone
from chatbot.price_drop_pilot_conversation_v14 import VERSION, prepare_initial_message
from chatbot.price_drop_pilot_guard_v13 import VERSION as V13_VERSION
from chatbot.property_lookup import PROPERTY_COLLECTION_NAME, get_prop_location, get_prop_operation
from chatbot.storage import get_db
from chatbot.whatsapp_client import send_whatsapp_message_detailed_sync

logger = logging.getLogger(__name__)
PROPERTY_CODE = "6436"
OLD_UF = 2790.0
NEW_UF = 2690.0
TEST_ID = "20261104-6436-price-drop-pilot-v13-regression-single-test"
V14_TEST_ID = "20261105-6436-price-drop-pilot-v14-phone-test-single"


def _blocked(reason: str) -> int:
    """Report a safe, non-PII reason for a fail-closed preflight."""
    logger.warning("[PRICE_DROP_PILOT_TEST] blocked reason=%s", reason)
    print(f"BLOCKED: {reason}")
    return 2


def run_test_once(*, send: bool = False, version: str | None = None) -> int:
    """One durable provider attempt at most, even after Render restarts."""
    campaign_version = version or os.environ.get("PRICE_DROP_PILOT_TEST_VERSION") or V13_VERSION
    if campaign_version not in {V13_VERSION, VERSION}:
        return _blocked("unsupported_version")
    phone = normalize_phone_strict(os.environ.get(TEST_PHONE, ""))
    if not phone:
        return _blocked("recipient_not_configured")
    if os.environ.get(FLAG, "").lower() not in {"1", "true", "yes"}:
        return _blocked("pilot_disabled")

    db = get_db()
    # The test never reads or edits existing leads. Historic duplicate identities
    # are intentionally irrelevant to this separate test conversation.
    if not is_test_phone(phone):
        return _blocked("recipient_not_allowlisted")

    prop = db[PROPERTY_COLLECTION_NAME].find_one(
        {"codigo": PROPERTY_CODE},
        {"codigo": 1, "disponible_prop360": 1, "estado.oficina": 1,
         "tipo": 1, "tipo_propiedad": 1, "tipo_operacion.tipo": 1,
         "comuna": 1, "ubicacion.comuna": 1,
         "tipo_operacion.venta": 1, "tipo_operacion.precio_venta.precio_uf": 1},
    ) or {}
    sale = (prop.get("tipo_operacion") or {})
    current = (sale.get("precio_venta") or {}).get("precio_uf")
    if (
        not prop.get("disponible_prop360")
        or (prop.get("estado") or {}).get("oficina") != "PROCASA SUCRE"
        or sale.get("venta") is not True
        or not isinstance(current, (int, float))
        or abs(float(current) - NEW_UF) > 0.01
        or not (2000 <= NEW_UF <= 5000 and OLD_UF > NEW_UF)
    ):
        return _blocked("listing_not_verified")

    inquiry_evidence = None
    if campaign_version == VERSION:
        property_type = get_prop_operation(prop).get("tipo")
        comuna = get_prop_location(prop).get("comuna")
        if (
            not isinstance(property_type, str) or not property_type.strip()
            or not isinstance(comuna, str) or not comuna.strip()
        ):
            return _blocked("listing_context_missing")

        inquiry_evidence = (
            "test-only roleplay: tester simulates a previous inquiry; not a CRM record"
        )
        preview = prepare_initial_message({
            "property_code": PROPERTY_CODE,
            "property_type": property_type,
            "comuna": comuna,
            "old_price_uf": OLD_UF,
            "current_price_uf": current,
            "new_price_uf": NEW_UF,
            "current_price_verified": True,
            "available_for_sale": True,
            "prior_inquiry_verified": True,
            "prior_inquiry_evidence": inquiry_evidence,
        })
        message = preview["message"]
        test_id = V14_TEST_ID
    else:
        # Preserve the established V13 sender as the default test version.
        message = (
            "Hola, ¿cómo estás? Soy de PROCASA 😊\n\n"
            "Te escribo porque el departamento de San Miguel tuvo una rebaja: "
            "ahora está en UF 2.690, 100 UF menos que antes (UF 2.790).\n\n"
            "Con este nuevo precio quizás valga la pena darle otra mirada. "
            "Acá está la publicación actualizada:\n"
            "https://www.procasa.cl/6436\n\n"
            "¿Cómo lo ves con este nuevo valor?"
        )
        test_id = TEST_ID

    key = "test:" + test_id + ":" + hashlib.sha256(phone.encode()).hexdigest()[:24]
    if db[COLLECTION].find_one({"_id": key}, {"_id": 1}):
        return _blocked("attempt_already_recorded")

    if not send:
        logger.info("[PRICE_DROP_PILOT_TEST] preflight_passed version=%s property=%s",
                    campaign_version, PROPERTY_CODE)
        print("DRY RUN PASSED: isolated recipient and current price verified; no WhatsApp sent")
        print(message)
        return 0

    now = datetime.now(timezone.utc)
    campaign = {
        "_id": key,
        "phone_normalized": phone,
        "test_mode": True,
        "property_code": PROPERTY_CODE,
        "old_price_uf": OLD_UF,
        "new_price_uf": NEW_UF,
        "operation": "Venta",
        "channel": "whatsapp",
        "campaign_version": campaign_version,
        "status": "SENDING",
        "isolation_version": "test_webhook_before_crm_v1",
        "created_at": now,
    }
    if campaign_version == VERSION:
        # This roleplay evidence exists only in the isolated test campaign.
        campaign.update({
            "outbound_message": message,
            "inquiry_evidence_at": inquiry_evidence,
        })
    db[COLLECTION].insert_one(campaign)
    # One provider attempt; timeout/unknown is never auto-retried.
    try:
        receipt = send_whatsapp_message_detailed_sync(phone, message)
    except Exception:
        db[COLLECTION].update_one({"_id": key}, {"$set": {
            "status": "DELIVERY_UNKNOWN",
            "attempted_at": now,
        }})
        print("DELIVERY_UNKNOWN: provider call raised an exception; no retry")
        logger.error("[PRICE_DROP_PILOT_TEST] status=delivery_unknown reason=provider_exception")
        return 1

    sent = bool(receipt.get("success") and receipt.get("provider_message_id")
                and not receipt.get("provider_call_uncertain"))
    db[COLLECTION].update_one({"_id": key}, {"$set": {
        "status": "SENT" if sent else "DELIVERY_UNKNOWN",
        "provider_message_id": receipt.get("provider_message_id"),
        "provider_status": receipt.get("delivery_status"),
        "attempted_at": now,
        **({"sent_at": now, "expires_at": now + timedelta(days=7)} if sent else {}),
    }})
    if not sent:
        print("DELIVERY_UNKNOWN: acceptance without durable provider ID or failed call; no retry")
        logger.error("[PRICE_DROP_PILOT_TEST] status=delivery_unknown reason=provider_unconfirmed")
        return 1
    print("SENT: provider accepted one test message; wait for inbound reply verification")
    logger.info("[PRICE_DROP_PILOT_TEST] status=provider_accepted version=%s property=%s",
                campaign_version, PROPERTY_CODE)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Perform one real provider attempt")
    args = parser.parse_args()
    return run_test_once(send=args.send)


if __name__ == "__main__":
    raise SystemExit(main())
