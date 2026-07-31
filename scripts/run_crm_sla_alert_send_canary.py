"""CRM SLA Alert Send Canary — manual single-alert delivery.

Usage:
  python scripts/run_crm_sla_alert_send_canary.py \
    --alert-id "2c688104-4fb9-46a4-b5d4-54936d14d089|breached|6989c6309dd2ba54e478196b" \
    --confirm "SEND_ONE_CRM_SLA_CANARY"
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

from chatbot.crm_metrics import coerce_utc_datetime, event_evidence, utc_now, calculate_sla
from chatbot.crm_sla_alert_repository import (
    COLLECTION, ST_PENDING, ST_PROCESSING, ST_SENT, ST_FAILED_RETRYABLE,
    ST_CANCELLED, ST_DELIVERY_UNCERTAIN,
    mark_delivery_started, finalize_alert, cancel_alert,
)
from chatbot.crm_sla_alert_templates import MESSAGE_DOMAIN, build_sla_message, build_lead_url, build_deadline_display
from chatbot.crm_sla_alert_evaluator import (
    SLA_STOP_RESULTS, _is_test_lead, CLOSED_STAGES, EXCLUDED_ORIGINS,
    classify_outreach_state, add_business_minutes,
    THRESHOLD_BREACHED_HOT, THRESHOLD_BREACHED_NORMAL,
    ALERT_LEVEL_WARNING, ALERT_LEVEL_BREACHED, SLA_PROFILE_HOT, SLA_PROFILE_STANDARD,
)
from chatbot.storage import get_async_db
from chatbot.constants import CHILE_TZ

logging.basicConfig(level=logging.INFO, format="[SLA_CANARY_SEND] %(message)s")
logger = logging.getLogger("sla_canary_send")

REQUIRED_CONFIRM = "SEND_ONE_CRM_SLA_CANARY"


def mask_phone(p):
    if not p: return "<none>"
    d = "".join(c for c in p if c.isdigit())
    return f"+56 9 **** {d[-4:]}" if len(d) >= 4 else "<invalid>"


async def _revalidate_and_refresh(db, alert: dict, now_utc) -> dict | None:
    """Revalidate eligibility, recalc message. Returns updated alert dict or None if cancelled."""
    lead_id = alert.get("lead_id")
    cycle_id = alert.get("assignment_cycle_id")
    recipient = alert.get("recipient_user_id")

    if not all([lead_id, cycle_id, recipient]):
        await cancel_alert(db, alert_id=alert["_id"], reason="incomplete_alert_data")
        logger.warning("CANCELLED: incomplete data")
        return None

    # Convert lead_id to ObjectId if needed (leads collection uses ObjectId)
    from bson import ObjectId
    lead_q = lead_id
    try:
        lead_q = ObjectId(lead_id)
    except Exception:
        pass

    lead = await db["leads"].find_one({"_id": lead_q}, {
        "pipeline_stage": 1, "lead_temperature_effective": 1,
        "ejecutivo_asignado": 1, "lead_origin": 1,
        "prospecto": 1, "lifecycle.hot_since": 1, "phone": 1,
    })
    if not lead:
        await cancel_alert(db, alert_id=alert["_id"], reason="lead_not_found")
        return None
    if _is_test_lead(lead):
        await cancel_alert(db, alert_id=alert["_id"], reason="test_lead")
        return None

    stage = str(lead.get("pipeline_stage") or "").upper()
    if stage in CLOSED_STAGES:
        await cancel_alert(db, alert_id=alert["_id"], reason="lead_closed")
        return None

    cycle = await db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle_id})
    if not cycle or cycle.get("cycle_status") != "active" or cycle.get("unassigned_at"):
        await cancel_alert(db, alert_id=alert["_id"], reason="cycle_inactive")
        return None

    assigned_at = coerce_utc_datetime(cycle.get("assigned_at"))
    if not assigned_at:
        await cancel_alert(db, alert_id=alert["_id"], reason="missing_assigned_at")
        return None

    if str(cycle.get("assigned_to_user_id") or "") != str(recipient):
        await cancel_alert(db, alert_id=alert["_id"], reason="reassigned")
        return None

    # Management check
    mgmt = await db["crm_management_results"].find({"assignment_cycle_id": cycle_id}).to_list(length=50)
    if any(str(m.get("result_type") or "").upper() in SLA_STOP_RESULTS
           and coerce_utc_datetime(m.get("occurred_at"))
           and coerce_utc_datetime(m.get("occurred_at")) >= assigned_at for m in mgmt):
        await cancel_alert(db, alert_id=alert["_id"], reason="management_completed")
        return None

    events = await db["crm_events"].find({"lead_id": lead_id, "timestamp": {"$gte": assigned_at}}).to_list(length=100)
    if any(event_evidence(e)["management"] for e in events):
        await cancel_alert(db, alert_id=alert["_id"], reason="management_completed")
        return None

    # Phone check
    executive = str(lead.get("ejecutivo_asignado") or "")
    user = await db["usuarios"].find_one({"nombre": executive, "is_active": True}, {"telefono": 1})
    if not user or not user.get("telefono"):
        await cancel_alert(db, alert_id=alert["_id"], reason="invalid_phone")
        return None
    phone = str(user["telefono"]).strip()

    # Recalculate SLA
    temp = str(lead.get("lead_temperature_effective") or "").upper()
    is_hot = temp == "HOT"
    hot_start = None
    if is_hot:
        hs = (lead.get("lifecycle") or {}).get("hot_since")
        hot_start = coerce_utc_datetime(hs) if hs else None
    sla = calculate_sla(assigned_at=assigned_at, now=now_utc, temperature=temp, hot_started_at=hot_start)

    sla_status = sla["status"]
    if sla_status not in ("near_critical", "critical"):
        await cancel_alert(db, alert_id=alert["_id"], reason="no_longer_eligible")
        return None

    new_level = ALERT_LEVEL_BREACHED if sla_status == "critical" else ALERT_LEVEL_WARNING
    sla_profile = SLA_PROFILE_HOT if is_hot else SLA_PROFILE_STANDARD

    # Effective start for elapsed
    effective_start = hot_start if (is_hot and hot_start) else assigned_at
    elapsed = int(max(0, sla.get("hot_minutes" if is_hot else "minutes", 0) or 0))

    # Deadline (keep original deadline_at from the document)
    deadline_dt = alert.get("deadline_at")
    if hasattr(deadline_dt, "astimezone"):
        deadline_display = build_deadline_display(deadline_dt, CHILE_TZ)
    else:
        deadline_threshold = THRESHOLD_BREACHED_HOT if is_hot else THRESHOLD_BREACHED_NORMAL
        deadline_dt = add_business_minutes(effective_start, deadline_threshold)
        deadline_display = build_deadline_display(deadline_dt, CHILE_TZ)

    # Recalculate outreach
    outreach_state = classify_outreach_state(events, assigned_at=assigned_at)

    # Rebuild message
    prospecto = lead.get("prospecto") or {}
    client_name = str(prospecto.get("nombre") or "Cliente").strip()
    client_name = client_name.split()[0] if client_name and client_name != "Cliente" else client_name
    property_code = str(prospecto.get("codigo") or lead.get("property_code") or "S/N")
    lead_url = build_lead_url(lead)
    message = build_sla_message(
        hot=is_hot, breached=(new_level == ALERT_LEVEL_BREACHED),
        client_first_name=client_name, property_code=property_code,
        elapsed_minutes=elapsed, deadline_display=deadline_display,
        lead_url=lead_url, outreach_state=outreach_state,
    )

    # Update the document via CAS on lease
    updated = await db[COLLECTION].find_one_and_update(
        {"_id": alert["_id"], "state": ST_PROCESSING, "lease_owner": alert.get("lease_owner")},
        {"$set": {
            "alert_level": new_level, "sla_profile": sla_profile,
            "elapsed_business_minutes": elapsed, "outreach_state": outreach_state,
            "rendered_message": message, "recipient_phone_snapshot": phone,
            "updated_at": now_utc,
        }},
    )
    if not updated:
        logger.warning("Lost lease during refresh — aborting send")
        return None

    return {**updated, "rendered_message": message, "recipient_phone_snapshot": phone}


async def send_one(alert_id: str):
    db = get_async_db()
    now_utc = utc_now()

    # LIVE_SEND guard
    from chatbot.crm_sla_alert_settings import CRM_SLA_ALERTS_LIVE_SEND
    if not CRM_SLA_ALERTS_LIVE_SEND:
        logger.error("CRM_SLA_ALERTS_LIVE_SEND is false — cannot send")
        return {"status": "send_disabled", "reason": "CRM_SLA_ALERTS_LIVE_SEND is false"}

    # Validate alert exists and is pending
    alert = await db[COLLECTION].find_one({
        "_id": alert_id, "message_domain": MESSAGE_DOMAIN, "state": ST_PENDING,
    })
    if not alert:
        logger.error("Alert not found or not pending: %s", alert_id)
        return {"status": "not_found"}

    # Check allowlist
    from chatbot.crm_sla_alert_settings import CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS
    if alert.get("recipient_user_id") not in CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS:
        logger.error("Recipient not in canary allowlist")
        return {"status": "not_authorized"}

    logger.info("Alert found: lead=%s cyc=%s level=%s mins=%s",
                alert.get("lead_id"), alert.get("assignment_cycle_id"),
                alert.get("alert_level"), alert.get("elapsed_business_minutes"))

    # Claim atomically
    worker_id = f"canary:{os.getpid()}"
    claimed = await db[COLLECTION].find_one_and_update(
        {"_id": alert_id, "state": ST_PENDING, "message_domain": MESSAGE_DOMAIN},
        {"$set": {
            "state": ST_PROCESSING, "lease_owner": worker_id,
            "lease_expires_at": now_utc + __import__("datetime").timedelta(seconds=120),
            "claimed_at": now_utc, "updated_at": now_utc,
        }, "$inc": {"attempt_count": 1}},
    )
    if not claimed:
        logger.error("Could not claim alert — another worker may have it")
        return {"status": "claim_failed"}

    # Revalidate + refresh message
    refreshed = await _revalidate_and_refresh(db, alerted := {
        **alert, "state": ST_PROCESSING, "lease_owner": worker_id,
    }, now_utc)
    if not refreshed:
        return {"status": "cancelled_or_lost_lease"}

    # Mark delivery started
    started = await mark_delivery_started(db, alert_id=alert_id, worker_id=worker_id)
    if not started:
        await db[COLLECTION].update_one(
            {"_id": alert_id, "state": ST_PROCESSING},
            {"$set": {"state": ST_PENDING, "lease_owner": None, "lease_expires_at": None, "updated_at": utc_now()}},
        )
        return {"status": "delivery_start_failed"}
    attempt_id = started.get("delivery_attempt_id")

    # Call provider (deferred import, only when LIVE_SEND=true)
    phone = refreshed.get("recipient_phone_snapshot", alert.get("recipient_phone_snapshot", ""))
    message = refreshed.get("rendered_message", alert.get("rendered_message", ""))

    logger.info("Sending to %s: message[%d chars]", mask_phone(phone), len(message))
    try:
        from chatbot.whatsapp_client import send_whatsapp_message_detailed

        # send_whatsapp_message_detailed is async
        result = await asyncio.wait_for(
            send_whatsapp_message_detailed(phone, message),
            timeout=30,
        )
    except asyncio.TimeoutError:
        # Timeout after delivery_started → delivery_uncertain (never re-send)
        await finalize_alert(db, alert_id=alert_id, state=ST_DELIVERY_UNCERTAIN,
                             error="provider_timeout", delivery_outcome="timeout",
                             delivery_attempt_id=attempt_id)
        logger.error("Provider timeout — marked delivery_uncertain")
        return {"status": "delivery_uncertain", "reason": "timeout"}
    except Exception as exc:
        await finalize_alert(db, alert_id=alert_id, state=ST_DELIVERY_UNCERTAIN,
                             error=str(exc), delivery_outcome="crash_after_start",
                             delivery_attempt_id=attempt_id)
        logger.error("Provider error: %s", exc)
        return {"status": "delivery_uncertain", "reason": str(exc)}

    if result.get("success") and result.get("provider_message_id"):
        await finalize_alert(db, alert_id=alert_id, state=ST_SENT,
                             provider_message_id=result["provider_message_id"],
                             delivery_attempt_id=attempt_id)
        logger.info("SENT: provider_message_id=%s", result["provider_message_id"])
        return {"status": "sent", "provider_message_id": result["provider_message_id"]}
    else:
        await finalize_alert(db, alert_id=alert_id, state=ST_FAILED_RETRYABLE,
                             error=result.get("error", "send_failed"),
                             delivery_attempt_id=attempt_id)
        logger.error("Send failed (rejected)")
        return {"status": "failed_retryable"}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--alert-id", required=True)
    p.add_argument("--confirm", required=True)
    args = p.parse_args()

    if args.confirm != REQUIRED_CONFIRM:
        print("ERROR: --confirm must be exactly 'SEND_ONE_CRM_SLA_CANARY'. Zero claims, zero provider calls.")
        sys.exit(1)

    print(f"Alert ID: {args.alert_id}")
    result = asyncio.run(send_one(args.alert_id))
    print(f"Result: {result}")
