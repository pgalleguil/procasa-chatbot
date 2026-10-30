"""Single-recipient, opt-in price-drop WhatsApp pilot sender.

Never use this for bulk sending. Dry-run by default. Run from the trusted
application environment only after enabling the isolated test feature.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatbot.phone_utils import normalize_phone_strict
from chatbot.price_drop_pilot import COLLECTION, FLAG, TEST_PHONE
from chatbot.price_drop_pilot_isolation import is_test_phone
from chatbot.property_lookup import PROPERTY_COLLECTION_NAME
from chatbot.storage import get_db
from chatbot.whatsapp_client import send_whatsapp_message_detailed_sync

PROPERTY_CODE = "6436"
OLD_UF = 2790.0
NEW_UF = 2690.0
TEST_ID = "20261030-6436-price-drop-pilot-v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Perform one real provider attempt")
    args = parser.parse_args()

    phone = normalize_phone_strict(os.environ.get(TEST_PHONE, ""))
    if not phone:
        print("BLOCKED: test recipient is not configured")
        return 2
    if os.environ.get(FLAG, "").lower() not in {"1", "true", "yes"}:
        print("BLOCKED: isolated pilot flag is disabled")
        return 2

    db = get_db()
    # The test never reads or edits existing leads. Historic duplicate identities
    # are intentionally irrelevant to this separate test conversation.
    if not is_test_phone(phone):
        print("BLOCKED: recipient is outside isolated test allowlist")
        return 2

    prop = db[PROPERTY_COLLECTION_NAME].find_one(
        {"codigo": PROPERTY_CODE},
        {"codigo": 1, "disponible_prop360": 1, "estado.oficina": 1,
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
        print("BLOCKED: sale availability or current price differs from the verified pilot")
        return 2

    key = "test:" + TEST_ID + ":" + hashlib.sha256(phone.encode()).hexdigest()[:24]
    if db[COLLECTION].find_one({"_id": key}, {"_id": 1}):
        print("BLOCKED: an attempt for this test was already recorded; no retries")
        return 2

    message = (
        "PRUEBA INTERNA PROCASA\n"
        "Estamos probando una comunicación de rebaja de precio. "
        "El departamento en VENTA de San Miguel (código 6436) bajó "
        "de UF 2.790 a UF 2.690.\n"
        "https://www.procasa.cl/6436\n"
        "Responde 'Sí, me interesa' para comprobar que el chatbot "
        "recuerda a qué propiedad se refiere el mensaje."
    )
    if not args.send:
        print("DRY RUN PASSED: isolated recipient and current price verified; no WhatsApp sent")
        print(message)
        return 0

    now = datetime.now(timezone.utc)
    db[COLLECTION].insert_one({
        "_id": key,
        "phone_normalized": phone,
        "test_mode": True,
        "property_code": PROPERTY_CODE,
        "old_price_uf": OLD_UF,
        "new_price_uf": NEW_UF,
        "operation": "Venta",
        "channel": "whatsapp",
        "campaign_version": "price_drop_pilot_v1",
        "status": "SENDING",
        "isolation_version": "test_webhook_before_crm_v1",
        "created_at": now,
    })
    # One provider attempt; timeout/unknown is never auto-retried.
    try:
        receipt = send_whatsapp_message_detailed_sync(phone, message)
    except Exception:
        db[COLLECTION].update_one({"_id": key}, {"$set": {
            "status": "DELIVERY_UNKNOWN",
            "attempted_at": now,
        }})
        print("DELIVERY_UNKNOWN: provider call raised an exception; no retry")
        return 1

    sent = bool(receipt.get("success") and receipt.get("provider_message_id"))
    db[COLLECTION].update_one({"_id": key}, {"$set": {
        "status": "SENT" if sent else "DELIVERY_UNKNOWN",
        "provider_message_id": receipt.get("provider_message_id"),
        "provider_status": receipt.get("delivery_status"),
        "attempted_at": now,
        **({"sent_at": now, "expires_at": now + timedelta(days=7)} if sent else {}),
    }})
    if not sent:
        print("DELIVERY_UNKNOWN: acceptance without durable provider ID or failed call; no retry")
        return 1
    print("SENT: provider accepted one test message; wait for inbound reply verification")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
