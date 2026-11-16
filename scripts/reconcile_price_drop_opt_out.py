"""Repair one misclassified, explicitly opted-out price-drop reply on the canonical lead.

Read-only by default. No WhatsApp sends, campaign approvals, or lead reassignment.
Run from the repo root with PRICE_DROP_ENV_FILE set:
    python -m scripts.reconcile_price_drop_opt_out --phone +569... --execute
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from chatbot.phone_utils import normalize_phone_strict, phone_identity_variants
from chatbot.price_drop_campaign_classifier import classify_price_drop_reply


class ReconciliationBlocked(ValueError):
    """Do not write if identity, provenance or active state is ambiguous."""


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ReconciliationBlocked("missing_original_customer_reply_timestamp")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def reconcile_one(db: Any, *, phone: str, execute: bool = False) -> dict:
    if db.name != "URLS":
        raise ReconciliationBlocked("unexpected_database")
    canonical = normalize_phone_strict(phone)
    if not canonical:
        raise ReconciliationBlocked("invalid_phone")
    variants = list(phone_identity_variants(canonical))
    leads = list(db["leads"].find({
        "$or": [
            {"phone": {"$in": variants}},
            {"contact_phone_normalized": {"$in": variants}},
        ],
    }, {"phone": 1, "contact_phone_normalized": 1, "price_drop_v2": 1}).limit(2))
    if len(leads) != 1:
        raise ReconciliationBlocked("lead_not_unique_or_not_found")
    lead = leads[0]
    v2 = lead.get("price_drop_v2") or {}
    campaigns = v2.get("campaigns") or []
    if v2.get("opted_out") is True:
        return {"status": "ALREADY_OPTED_OUT", "lead_last6": str(lead["_id"])[-6:]}
    candidates = []
    for i, campaign in enumerate(campaigns):
        if campaign.get("status") != "RESPONDED":
            continue
        text = str(campaign.get("last_customer_text") or "").strip()
        provider_id = str(campaign.get("last_inbound_provider_id") or "").strip()
        campaign_id = str(campaign.get("campaign_id") or "").strip()
        if not text or not provider_id or not campaign_id:
            continue
        classification = classify_price_drop_reply(text)
        if classification.label != "BAJA":
            continue
        old_label = (campaign.get("last_classification") or {}).get("label")
        if old_label == "BAJA":
            continue
        matches = [(j, event) for j, event in enumerate(v2.get("reply_events") or [])
                   if (event.get("campaign_id") == campaign_id
                       and event.get("provider_message_id") == provider_id
                       and str(event.get("text") or "").strip() == text)]
        if len(matches) != 1:
            raise ReconciliationBlocked("campaign_reply_event_missing_or_ambiguous")
        if provider_id not in (v2.get("processed_inbound_provider_ids") or []):
            raise ReconciliationBlocked("provider_id_not_previously_processed")
        j, event = matches[0]
        stamp = _utc(event.get("received_at") or campaign.get("last_customer_reply_at"))
        candidates.append((i, j, campaign, event, classification.as_dict(), stamp, old_label))
    if len(candidates) != 1:
        raise ReconciliationBlocked("expected_exactly_one_misclassified_opt_out")
    i, j, campaign, event, classified, stamp, old_label = candidates[0]
    campaign_id = campaign["campaign_id"]
    provider_id = campaign["last_inbound_provider_id"]
    text = campaign["last_customer_text"]
    summary = {
        "status": "READY_TO_CORRECT" if not execute else "PENDING",
        "lead_last6": str(lead["_id"])[-6:],
        "phone_last4": canonical[-4:],
        "property_code": campaign.get("property_code"),
        "campaign_status_before": campaign["status"],
        "classification_before": old_label,
        "classification_after": "BAJA",
        "outbound_messages_sent": 0,
    }
    if not execute:
        return summary

    # One atomic, conditional lead update; preserve the prior event label for audit.
    # Do not resend/notify, increment reply_count, create leads, or alter assignments.
    coll = db["leads"]
    result = coll.update_one({
        "_id": lead["_id"],
        "price_drop_v2.opted_out": {"$ne": True},
        f"price_drop_v2.campaigns.{i}.campaign_id": campaign_id,
        f"price_drop_v2.campaigns.{i}.status": "RESPONDED",
        f"price_drop_v2.campaigns.{i}.last_customer_text": text,
        f"price_drop_v2.campaigns.{i}.last_inbound_provider_id": provider_id,
        f"price_drop_v2.campaigns.{i}.last_classification.label": old_label,
        f"price_drop_v2.reply_events.{j}.campaign_id": campaign_id,
        f"price_drop_v2.reply_events.{j}.provider_message_id": provider_id,
        f"price_drop_v2.reply_events.{j}.text": event["text"],
    }, {
        "$set": {
            "price_drop_v2.opted_out": True,
            "price_drop_v2.opted_out_at": stamp,
            "price_drop_campaign_v1.opted_out": True,
            "conversation_status": "STOPPED_BY_CLIENT",
            "conversation_status_reason": "explicit_opt_out",
            "conversation_status_evidence": text[:300],
            "conversation_status_at": stamp.isoformat(),
            "conversation_status_version": "commercial_close_no_contact_v1",
            f"price_drop_v2.campaigns.{i}.status": "OPTED_OUT",
            f"price_drop_v2.campaigns.{i}.opted_out_at": stamp,
            f"price_drop_v2.campaigns.{i}.last_classification": classified,
            f"price_drop_v2.campaigns.{i}.notifications.administrator.status": "CANCELED",
            f"price_drop_v2.campaigns.{i}.notifications.property_executive.status": "FROZEN_ADMIN_ONLY",
            f"price_drop_v2.campaigns.{i}.notifications.administrator_info.status": "PENDING_INFO",
            f"price_drop_v2.campaigns.{i}.notifications.administrator_info.reason": "explicit_opt_out",
            f"price_drop_v2.reply_events.{j}.classification_original": event.get("classification"),
            f"price_drop_v2.reply_events.{j}.classification": classified,
        },
        "$push": {"price_drop_v2.classification_corrections": {
            "campaign_id": campaign_id,
            "provider_message_id": provider_id,
            "previous_label": old_label,
            "corrected_label": "BAJA",
            "source": "verified_historical_customer_opt_out",
            "corrected_at": datetime.now(timezone.utc),
        }},
    })
    if result.modified_count != 1:
        raise ReconciliationBlocked("concurrent_change_no_update_performed")
    check = coll.find_one({"_id": lead["_id"]}, {
        "conversation_status": 1, "price_drop_v2.opted_out": 1,
        "price_drop_v2.campaigns": 1,
    }) or {}
    confirmed = (check.get("conversation_status") == "STOPPED_BY_CLIENT"
                 and (check.get("price_drop_v2") or {}).get("opted_out") is True
                 and any(x.get("campaign_id") == campaign_id and x.get("status") == "OPTED_OUT"
                         for x in (check.get("price_drop_v2") or {}).get("campaigns") or []))
    if not confirmed:
        raise ReconciliationBlocked("write_not_confirmed_manual_review_required")
    summary["status"] = "OPTED_OUT_CONFIRMED"
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", required=True, help="Customer WhatsApp phone; never stored in this script")
    parser.add_argument("--execute", action="store_true", help="Update exactly one verified lead")
    args = parser.parse_args()
    try:
        # Import only here: this loader reads PRICE_DROP_ENV_FILE just like the
        # existing price-drop CLI, avoiding any credentials in this repository.
        from scripts.enviar_piloto_rebajas import _get_db
        result = reconcile_one(_get_db(), phone=args.phone, execute=args.execute)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ReconciliationBlocked as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
