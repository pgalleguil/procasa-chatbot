"""Idempotent reconciliation for the 2026-07-26 commercial notification queue.

Dry-run is the default. --apply performs MongoDB writes but never calls a
provider or the chatbot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from bson import ObjectId
from pymongo import ReturnDocument

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import Config
from chatbot.business_calendar import CHILE, next_business_slot_utc
from chatbot.crm_hot_delivery import assign_and_enqueue_hot
from chatbot.crm_metrics import create_assignment_cycle, event_evidence
from chatbot.crm_non_hot_digest import accumulate_non_hot_lead
from chatbot.lead_router import find_responsible_executive
from chatbot.storage import get_db


TARGET_IDS = (
    "6a3166b1ffd1406f99859012", "6a619ab0c20786d0d43cbbbb",
    "6a640514c20786d0d43cc36a", "6a64110ec20786d0d43cc390",
    "6a64c717c20786d0d43cc4e2", "6a64d620c20786d0d43cc50b",
    "6a659e17c20786d0d43cc6e0", "6a6631e6c20786d0d43cc7f9",
    "6a6637eac20786d0d43cc802",
)
TECHNICAL_REASONS = {
    "historical_reconciliation", "lead_processed", "lead_processed_repair",
    "startup", "startup_repair", "cycle_repair", "backfill",
    "reconciliation", "deploy_reprocessing",
}
WEEKEND_START = datetime(2026, 7, 24, 23, 0, tzinfo=timezone.utc)
MONDAY_OPEN = next_business_slot_utc(
    CHILE.localize(datetime(2026, 7, 26, 12, 0))
)


def _aware(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _latest_event(db, lead):
    lead_id = lead["_id"]
    jobs = list(db["chatbot_inbound_jobs"].find({
        "phone": lead.get("phone"),
        "received_at": {"$gte": WEEKEND_START.replace(tzinfo=None)},
        "inbound_provider_message_id": {"$exists": True, "$ne": None},
    }).sort("received_at", -1))
    for job in jobs:
        provider_id = str(job.get("inbound_provider_message_id") or "")
        if provider_id.startswith("synthetic_canary"):
            continue
        batch = db["chatbot_inbound_jobs"].find_one({"_id": job.get("batch_id")}) or {}
        outbound = batch.get("outbound_provider_message_id")
        return {
            "id": provider_id,
            "reason": "inbound_message",
            "at": _aware(job.get("received_at")),
            "responded": job.get("state") == "responded" and bool(outbound),
            "outbound_provider_message_id": outbound,
            "batch_id": str(job.get("batch_id") or ""),
        }
    events = list(db["crm_events"].find({
        "lead_id": {"$in": [lead_id, str(lead_id)]},
        "type": {"$in": ["msg_in", "MANUAL_LEAD_CREATED"]},
        "timestamp": {"$gte": WEEKEND_START.replace(tzinfo=None)},
    }).sort("timestamp", -1))
    if events:
        event = events[0]
        return {
            "id": str(event["_id"]),
            "reason": "manual_lead_created" if event.get("type") == "MANUAL_LEAD_CREATED" else "inbound_message",
            "at": _aware(event.get("timestamp")),
            "responded": False,
        }
    return None


def _has_management(db, lead_id):
    events = db["crm_events"].find({"lead_id": {"$in": [lead_id, str(lead_id)]}})
    return any(event_evidence(event).get("management") for event in events)


def _recipient(db, executive):
    if not executive:
        return None
    return db["usuarios"].find_one(
        {"nombre": executive, "is_active": {"$ne": False}},
        {"_id": 1, "nombre": 1, "telefono": 1, "tel": 1, "movil": 1},
    )


def _ensure_assignment(db, lead, event, *, apply):
    existing = db["crm_assignment_cycles"].find_one({
        "lead_id": lead["_id"], "source_event_id": event["id"],
        "notification_eligible": True,
    })
    if existing:
        return existing, False

    executive = lead.get("ejecutivo_asignado") or (lead.get("prospecto") or {}).get("ejecutivo")
    recipient = _recipient(db, executive)
    if not recipient:
        if not apply:
            return None, False
        prospect = lead.get("prospecto") or {}
        executive, _phone, _kind = find_responsible_executive(
            property_code=prospect.get("codigo") or lead.get("codigo"),
            comuna=prospect.get("comuna") or lead.get("comuna"),
            zone=lead.get("zone"),
            lead_phone=lead.get("phone"),
            lead_name=prospect.get("nombre") or lead.get("nombre"),
        )
        recipient = _recipient(db, executive)
    if not recipient:
        return None, False
    if not apply:
        return {
            "assignment_cycle_id": "dry:" + hashlib.sha256(
                f"{lead['_id']}:{event['id']}".encode()
            ).hexdigest()[:20],
            "lead_id": lead["_id"], "assigned_to_user_id": str(recipient["_id"]),
            "assigned_to_display_name": recipient.get("nombre"),
            "reason": event["reason"], "cycle_origin": event["reason"],
            "notification_eligible": True, "cycle_status": "active",
        }, True

    db["crm_assignment_cycles"].update_many({
        "lead_id": lead["_id"], "cycle_status": "active",
        "$or": [
            {"notification_eligible": {"$ne": True}},
            {"reason": {"$in": sorted(TECHNICAL_REASONS)}},
        ],
    }, {"$set": {
        "cycle_status": "closed", "unassigned_at": event["at"],
        "closed_reason": "superseded_by_original_commercial_event",
    }})
    cycle = create_assignment_cycle(
        db, lead=lead, assigned_to_user_id=str(recipient["_id"]),
        assigned_by="weekend_reconciliation", reason=event["reason"],
        assigned_at=event["at"], assigned_to_display_name=recipient.get("nombre"),
    )
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": cycle["assignment_cycle_id"]},
        {"$set": {
            "source_event_id": event["id"],
            "source_inbound_provider_id": event["id"],
            "cycle_origin": "manual_lead" if event["reason"] == "manual_lead_created" else "inbound_message",
            "notification_eligible": True,
        }},
    )
    db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
        "ejecutivo_asignado": recipient.get("nombre"),
        "prospecto.ejecutivo": recipient.get("nombre"),
        "lifecycle.current_assignment_cycle_id": cycle["assignment_cycle_id"],
        "lifecycle.assigned_at": event["at"],
    }})
    return db["crm_assignment_cycles"].find_one({
        "assignment_cycle_id": cycle["assignment_cycle_id"]
    }), True


def _suppress_technical_hot(db, *, apply):
    rows = []
    for notification in db["crm_notifications_v1"].find({
        "notification_type": "lead_assignment_hot",
        "state": {"$in": ["pending", "failed_retryable", "sending"]},
        "provider_message_id": {"$exists": False},
    }):
        cycle = db["crm_assignment_cycles"].find_one({
            "assignment_cycle_id": notification.get("assignment_cycle_id")
        }) or {}
        if cycle.get("reason") not in TECHNICAL_REASONS and cycle.get("notification_eligible") is True:
            continue
        rows.append(str(notification["_id"]))
        if apply:
            now = datetime.now(timezone.utc)
            db["crm_notifications_v1"].find_one_and_update({
                "_id": notification["_id"],
                "state": {"$in": ["pending", "failed_retryable", "sending"]},
                "provider_message_id": {"$exists": False},
                "actually_delivered": {"$ne": True},
            }, {
                "$set": {
                    "state": "suppressed",
                    "notification_eligible": False,
                    "provider_called": False,
                    "actually_delivered": False,
                    "reason": "ineligible_technical_cycle",
                    "dedupe_active": False,
                    "updated_at": now,
                },
                "$unset": {
                    "lease_owner": "", "lease_expires_at": "",
                    "next_attempt_at": "", "delivery_token": "",
                },
                "$push": {"history": {
                    "at": now, "from": notification.get("state"),
                    "to": "suppressed", "reason": "ineligible_technical_cycle",
                }},
            }, return_document=ReturnDocument.AFTER)
    return rows


def reconcile(*, apply=False):
    db = get_db()
    suppressed = _suppress_technical_hot(db, apply=apply)
    results = []
    for raw_id in TARGET_IDS:
        lead = db["leads"].find_one({"_id": ObjectId(raw_id)})
        event = _latest_event(db, lead) if lead else None
        stage = str((lead or {}).get("stage") or (lead or {}).get("pipeline_stage") or "").upper()
        managed = _has_management(db, lead["_id"]) if lead else False
        if not lead:
            results.append({"lead_id": raw_id, "result": "missing"})
            continue
        if raw_id == "6a6631e6c20786d0d43cc7f9":
            results.append({"lead_id": raw_id, "result": "excluded_test_canary"})
            continue
        if stage in {"GESTION", "CONTACTED", "ARCHIVED", "CLOSED_WON", "CLOSED_LOST", "REJECTED"} or managed:
            results.append({"lead_id": raw_id, "result": "excluded_managed_or_closed"})
            continue
        if not event:
            results.append({"lead_id": raw_id, "result": "blocked_no_original_commercial_event"})
            continue
        cycle, created = _ensure_assignment(db, lead, event, apply=apply)
        if not cycle:
            results.append({"lead_id": raw_id, "result": "blocked_no_current_recipient"})
            continue
        temperature = str(lead.get("lead_temperature_effective") or "COLD").upper()
        notification = None
        if apply:
            recipient = _recipient(db, cycle.get("assigned_to_display_name"))
            phone = str((recipient or {}).get("telefono") or (recipient or {}).get("tel") or (recipient or {}).get("movil") or "")
            if temperature == "HOT":
                notification = assign_and_enqueue_hot(
                    db, lead=lead, recipient_user_id=str(cycle["assigned_to_user_id"]),
                    recipient_phone=phone, payload={"lead_type": "LeadHotWhatsapp"},
                    assigned_by="weekend_reconciliation", reason=event["reason"],
                    assigned_at=event["at"], send_after=MONDAY_OPEN,
                    recipient_name=cycle.get("assigned_to_display_name"),
                ).get("notification")
            else:
                notification = accumulate_non_hot_lead(db, lead=lead, cycle=cycle)
                if notification:
                    db["crm_notifications_v1"].update_one(
                        {"_id": notification["_id"]},
                        {"$set": {
                            "send_after": MONDAY_OPEN,
                            "window_due_at": MONDAY_OPEN.isoformat(),
                            "notification_eligible": True,
                            "weekend_window_closed": True,
                        }},
                    )
        results.append({
            "lead_id": raw_id, "result": "scheduled_hot" if temperature == "HOT" else "scheduled_digest",
            "event_id": event["id"], "cycle_id": cycle.get("assignment_cycle_id"),
            "cycle_created": created,
            "notification_id": str(notification.get("_id")) if notification else None,
        })
    return {
        "apply": apply,
        "monday_open_utc": MONDAY_OPEN.isoformat(),
        "monday_open_chile": MONDAY_OPEN.astimezone(CHILE).isoformat(),
        "suppressed_hot_ids": sorted(suppressed),
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not Config.MONGO_URI:
        raise RuntimeError("MONGO_URI is required")
    print(json.dumps(reconcile(apply=args.apply), default=str, ensure_ascii=False))
