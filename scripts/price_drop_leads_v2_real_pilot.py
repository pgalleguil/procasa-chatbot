"""Manual, one-recipient-at-a-time release tool for the 2026 price-drop pilot.

Never runs as a web worker or on service startup. No changes outside
URLS.leads, except the provider call explicitly requested by --mode send-one.
The customer allowlist and consent evidence are NOT stored in source control.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from typing import Callable

from chatbot.price_drop_leads_v2 import (
    REAL_PILOT_PRICE_WINDOWS, REAL_PILOT_RECIPIENTS_FLAG,
    PilotBlocked, approve_one, prepare_one, preview_one, send_one,
)
from chatbot.price_drop_campaign_real import _lead_id


def _recipients() -> dict[str, str]:
    """Require the three distinct lead/property pairs for the reviewed pilot."""
    raw = os.getenv(REAL_PILOT_RECIPIENTS_FLAG, "")
    rows = [v.strip() for v in raw.split(";") if v.strip()]
    if len(rows) != 3:
        return {}
    result: dict[str, str] = {}
    for row in rows:
        tokens = [v.strip() for v in row.split(":")]
        if len(tokens) != 2:
            return {}
        lead_id, code = tokens
        if (not re.fullmatch(r"[0-9a-fA-F]{24}", lead_id)
                or code not in REAL_PILOT_PRICE_WINDOWS
                or lead_id.lower() in result or code in result.values()):
            return {}
        result[lead_id.lower()] = code
    return result if set(result.values()) == set(REAL_PILOT_PRICE_WINDOWS) else {}


def run_action(db, *, mode: str, code: str, campaign_id: str,
               operator: str = "", consent_reviewed: bool = False,
               consent_basis: str = "", confirmed_live_link: bool = False,
               confirm_action: str = "",
               sender: Callable[[str, str], dict] | None = None) -> dict:
    """Explicit action with no implicit progression through campaign states."""
    if mode not in {"status", "preview", "prepare", "approve", "send-one"}:
        return {"status": "BLOCKED", "reason": "invalid_mode"}
    if code not in REAL_PILOT_PRICE_WINDOWS:
        return {"status": "BLOCKED", "reason": "unapproved_property"}
    if not campaign_id or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{5,79}", campaign_id):
        return {"status": "BLOCKED", "reason": "invalid_campaign_id"}

    recipients = _recipients()
    if not recipients:
        return {"status": "BLOCKED", "reason": "three_recipient_allowlist_missing"}
    lead_id = next((lid for lid, prop in recipients.items() if prop == code), None)
    if not lead_id:
        return {"status": "BLOCKED", "reason": "lead_not_in_allowlist"}
    lead = db["leads"].find_one({"_id": _lead_id(lead_id)})
    if not lead:
        return {"status": "BLOCKED", "reason": "lead_not_found"}
    phone = lead.get("contact_phone_normalized") or lead.get("phone") or ""
    suffix = str(phone)[-4:]
    campaigns = ((lead.get("price_drop_v2") or {}).get("campaigns") or [])
    current = next((row for row in campaigns
                    if row.get("campaign_id") == campaign_id), None)
    if sum(1 for row in campaigns if row.get("campaign_id") == campaign_id) > 1:
        return {"status": "BLOCKED", "reason": "ambiguous_campaign_rows"}

    if mode == "status":
        return {
            "status": "FOUND", "recipient_last4": suffix,
            "property_code": code, "campaign_id": campaign_id,
            "campaign_state": current.get("status") if current else "NOT_PREPARED",
            "provider_message_id": current.get("provider_message_id") if current else None,
            "provider_status": current.get("provider_status") if current else None,
            "reply_count": current.get("reply_count", 0) if current else 0,
            "consent_reviewed": current.get("consent_reviewed", False) if current else False,
            "writes": 0, "sends": 0,
        }

    if mode in {"prepare", "approve", "send-one"}:
        expected = {"prepare": "PREPARE_", "approve": "APPROVE_",
                    "send-one": "SEND_"}[mode] + code
        if not str(operator).strip() or confirm_action != expected:
            return {"status": "BLOCKED", "reason": "explicit_operator_confirmation_required"}
        if not confirmed_live_link:
            return {"status": "BLOCKED", "reason": "published_link_not_verified"}

    if mode in {"preview", "prepare"} and current:
        return {"status": "EXISTING", "recipient_last4": suffix,
                "campaign_state": current.get("status"), "sends": 0}

    currency, old, new = REAL_PILOT_PRICE_WINDOWS[code]
    data = {
        "lead_id": lead_id, "phone": phone,
        "campaign_id": campaign_id, "property_code": code,
        "old_price_uf": old if currency == "UF" else None,
        "new_price_uf": new if currency == "UF" else None,
        "old_price_clp": old if currency == "CLP" else None,
        "new_price_clp": new if currency == "CLP" else None,
        "currency": currency, "test_mode": False,
        "consent_reviewed": consent_reviewed,
        "consent_basis": consent_basis,
    }
    try:
        if mode == "preview":
            preview = preview_one(db, **data)
            return {
                "status": "READY", "recipient_last4": suffix,
                "property_code": code,
                "inquiry_evidence_at": preview["inquiry_evidence_at"].isoformat(),
                "message": preview["message"], "writes": 0, "sends": 0,
            }
        if mode == "prepare":
            if not consent_reviewed or not str(consent_basis or "").strip():
                return {"status": "BLOCKED", "reason": "documented_consent_required"}
            result = prepare_one(db, **data)
            return {**result, "recipient_last4": suffix, "sends": 0}
        if mode == "approve":
            result = approve_one(db, lead_id=lead_id, campaign_id=campaign_id,
                                 operator=operator)
            return {**result, "recipient_last4": suffix, "sends": 0}
        # Sending requires all V2 runtime flags in addition to this CLI
        # confirmation. No attempt is automatically retried on uncertainty.
        result = send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                          sender=sender)
        return {**result, "recipient_last4": suffix}
    except (PilotBlocked, ValueError) as exc:
        return {"status": "BLOCKED", "reason": str(exc),
                "recipient_last4": suffix, "sends": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True,
                        choices=("status", "preview", "prepare", "approve", "send-one"))
    parser.add_argument("--property", required=True,
                        choices=tuple(REAL_PILOT_PRICE_WINDOWS))
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--operator", default="")
    parser.add_argument("--consent-reviewed", action="store_true")
    parser.add_argument("--consent-basis", default="")
    parser.add_argument("--confirmed-live-link", action="store_true")
    parser.add_argument("--confirm-action", default="")
    args = parser.parse_args()
    from chatbot.storage import get_db
    outcome = run_action(
        get_db(), mode=args.mode, code=args.property,
        campaign_id=args.campaign_id, operator=args.operator,
        consent_reviewed=args.consent_reviewed,
        consent_basis=args.consent_basis,
        confirmed_live_link=args.confirmed_live_link,
        confirm_action=args.confirm_action,
    )
    # No full recipient numbers, WhatsApp payloads or consent text in logs.
    print(json.dumps(outcome, ensure_ascii=False, default=str))
    return 0 if outcome.get("status") in {"FOUND", "READY", "PREPARED",
                                         "APPROVED", "SENT", "EXISTING"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
