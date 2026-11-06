"""One-shot V2 price-drop WhatsApp test limited to the authorized tester.

Only explicit Render startup gates can invoke this; normal deployments do not.
Only leads is written. Price and listing history are read-only. Never bulk send.
"""
from __future__ import annotations

import os
from typing import Callable

from chatbot.phone_utils import normalize_phone_strict
from chatbot.price_drop_leads_v2 import (
    APPROVED_ID_FLAG, CONTEXT_FLAG, SEND_FLAG, TEST_PHONE_FLAG,
    PilotBlocked, approve_one, prepare_one, preview_one, send_one,
)

BOOT_MODE = "PRICE_DROP_LEADS_V2_TEST_BOOTSTRAP_MODE"
BOOT_ID = "PRICE_DROP_LEADS_V2_TEST_BOOTSTRAP_ID"
BOOT_OPERATOR = "PRICE_DROP_LEADS_V2_TEST_OPERATOR"
TESTER_LAST4 = "9804"
CODE = "6436"
OLD = 2790
NEW = 2690


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "yes", "true"}


def run_bootstrap(db, *, sender: Callable[[str, str], dict] | None = None) -> dict:
    """Preflight or atomically claim one dispatch; no repeat on restart."""
    mode = os.getenv(BOOT_MODE, "off").strip().lower()
    if mode not in {"preview", "send"}:
        return {"status": "DISABLED"}

    campaign_id = os.getenv(BOOT_ID, "").strip()
    if not campaign_id.startswith("test_leads_6436_") or not (6 <= len(campaign_id) <= 80):
        return {"status": "BLOCKED", "reason": "invalid_test_campaign_id"}

    phone = normalize_phone_strict(os.getenv(TEST_PHONE_FLAG, ""))
    if not phone or phone[-4:] != TESTER_LAST4:
        return {"status": "BLOCKED", "reason": "test_recipient_not_allowlisted"}

    # Select exactly one canonical existing lead; do not merge or upsert.
    rows = list(db["leads"].find(
        {"phone": phone},
        {"_id": 1, "phone": 1, "price_drop_v2.campaigns": 1},
    ).limit(2))
    if len(rows) != 1:
        return {"status": "BLOCKED", "reason": "canonical_test_lead_ambiguous_or_missing"}

    lead = rows[0]
    lead_id = str(lead["_id"])
    entries = (lead.get("price_drop_v2") or {}).get("campaigns") or []
    matching = [c for c in entries if c.get("campaign_id") == campaign_id]
    if len(matching) > 1:
        return {"status": "BLOCKED", "reason": "duplicate_campaign_id"}

    if matching:
        state = matching[0].get("status")
        if state not in {"PREPARED", "APPROVED"}:
            # Sending, sent, replied or uncertain are never retried.
            return {"status": "ALREADY_ATTEMPTED", "campaign_id": campaign_id,
                    "campaign_state": state}
        if mode == "preview":
            return {"status": "EXISTING", "campaign_id": campaign_id,
                    "campaign_state": state}
    else:
        try:
            preview_one(
                db, lead_id=lead_id, phone=phone, campaign_id=campaign_id,
                property_code=CODE, old_price_uf=OLD, new_price_uf=NEW,
                test_mode=True,
            )
        except PilotBlocked as exc:
            return {"status": "BLOCKED", "reason": str(exc)}
        if mode == "preview":
            return {"status": "READY", "campaign_id": campaign_id,
                    "recipient_last4": TESTER_LAST4, "writes": 0, "sends": 0}

    # All explicit gates must exist before making any campaign write.
    operator = os.getenv(BOOT_OPERATOR, "").strip()
    if not (_enabled(SEND_FLAG) and _enabled(CONTEXT_FLAG)
            and os.getenv(APPROVED_ID_FLAG) == campaign_id and operator):
        return {"status": "BLOCKED", "reason": "send_gates_not_armed"}

    try:
        if not matching:
            prepare_one(
                db, lead_id=lead_id, phone=phone, campaign_id=campaign_id,
                property_code=CODE, old_price_uf=OLD, new_price_uf=NEW,
                test_mode=True,
            )
            state = "PREPARED"
        if state == "PREPARED":
            approve_one(db, lead_id=lead_id, campaign_id=campaign_id,
                        operator=operator)
        outcome = send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                           sender=sender)
    except PilotBlocked as exc:
        return {"status": "BLOCKED", "reason": str(exc)}

    # Do not log recipient, message text or external provider payload.
    return {"status": outcome.get("status", "UNKNOWN"),
            "campaign_id": campaign_id,
            "recipient_last4": TESTER_LAST4,
            "provider_message_id": outcome.get("provider_message_id")}
