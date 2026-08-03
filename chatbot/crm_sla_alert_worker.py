"""CRM SLA Alert Worker — exclusive processing for crm_sla_alerts_v1.

Safe send order:
1. claim_next_alert (atomic)
2. revalidate
3. mark_delivery_started (lease-gated: _id + processing + lease_owner + lease not expired)
4. only if step 3 succeeded → call sender
5. finalize conditioned on same delivery_attempt_id

After delivery_started:
- only explicit rejection before acceptance → retryable
- timeout / disconnect / ambiguous → delivery_uncertain (never re-sent)
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter

from bson import ObjectId

from .crm_metrics import (
    calculate_sla, coerce_utc_datetime, event_evidence, utc_now,
)
from .crm_sla_alert_evaluator import (
    SLA_STOP_RESULTS, _is_test_lead, CLOSED_STAGES, EXCLUDED_ORIGINS,
)
from .crm_sla_alert_repository import (
    COLLECTION, ST_PENDING, ST_PROCESSING, ST_SENT, ST_FAILED_RETRYABLE,
    ST_FAILED_FINAL, ST_CANCELLED, ST_DELIVERY_UNCERTAIN,
    claim_next_alert, mark_delivery_started, finalize_alert,
    cancel_alert, cancel_alerts_for_cycle,
)
from .crm_sla_alert_sender import SenderResult, get_sender, SlaAlertSender
from .crm_sla_alert_settings import (
    CRM_SLA_ALERTS_ENABLED,
    MAX_PER_RUN,
    MAX_PER_RECIPIENT_PER_RUN,
    PROVIDER_TIMEOUT_SECONDS,
    validate_live_send_config,
)
from .storage import get_async_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Re-validation
# ---------------------------------------------------------------------------

async def _revalidate(db, alert: dict) -> str | None:
    lead_id = alert.get("lead_id")
    cycle_id = alert.get("assignment_cycle_id")
    recipient = alert.get("recipient_user_id")
    alert_level = alert.get("alert_level")
    if not all([lead_id, cycle_id, recipient]):
        return "incomplete_alert_data"

    lead_query_ids = [lead_id]
    lead_query = {"_id": lead_id}
    lead = await db["leads"].find_one(lead_query, {
        "pipeline_stage": 1, "stage": 1, "lead_temperature_effective": 1,
        "ejecutivo_asignado": 1, "phone": 1, "lead_origin": 1, "origin": 1,
        "prospecto.origen": 1, "lifecycle.hot_since": 1,
    })
    try:
        lead_query_ids.append(ObjectId(lead_id))
    except Exception:
        pass
    if not lead and len(lead_query_ids) > 1:
        lead = await db["leads"].find_one({"_id": lead_query_ids[1]}, {
            "pipeline_stage": 1, "stage": 1, "lead_temperature_effective": 1,
            "ejecutivo_asignado": 1, "phone": 1, "lead_origin": 1, "origin": 1,
            "prospecto.origen": 1, "lifecycle.hot_since": 1,
        })
    if not lead:
        return "lead_not_found"
    if _is_test_lead(lead):
        return "test_or_synthetic_lead"
    stage = str(lead.get("pipeline_stage") or lead.get("stage", "")).upper()
    if stage in CLOSED_STAGES:
        return "lead_closed"

    cycle = await db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle_id})
    if not cycle:
        return "cycle_not_found"
    if cycle.get("cycle_status") != "active" or cycle.get("unassigned_at"):
        return "cycle_inactive"

    assigned_at = coerce_utc_datetime(cycle.get("assigned_at"))
    if not assigned_at:
        return "missing_assigned_at"
    if str(cycle.get("assigned_to_user_id") or "") != str(recipient):
        return "reassigned"

    mgmt = await db["crm_management_results"].find({"assignment_cycle_id": cycle_id}).to_list(length=50)
    if any(str(m.get("result_type") or "").upper() in SLA_STOP_RESULTS
           and coerce_utc_datetime(m.get("occurred_at"))
           and coerce_utc_datetime(m.get("occurred_at")) >= assigned_at for m in mgmt):
        return "management_completed"

    events = await db["crm_events"].find({"lead_id": lead_id, "timestamp": {"$gte": assigned_at}}).to_list(length=100)
    if len(lead_query_ids) > 1:
        events += await db["crm_events"].find({"lead_id": lead_query_ids[1], "timestamp": {"$gte": assigned_at}}).to_list(length=100)
    if any(event_evidence(e)["management"] for e in events):
        return "management_completed"

    reason = str(cycle.get("reason") or "").lower()
    if reason in EXCLUDED_ORIGINS:
        return f"excluded_origin:{reason}"
    if str(cycle.get("cycle_origin") or "").lower() in EXCLUDED_ORIGINS:
        return f"excluded_cycle_origin:{cycle.get('cycle_origin')}"

    if alert_level == "warning" and not alert.get("delivery_started_at"):
        temp = str(lead.get("lead_temperature_effective") or "").upper()
        hot_start = None
        if temp == "HOT":
            hs = (lead.get("lifecycle") or {}).get("hot_since")
            hot_start = coerce_utc_datetime(hs) if hs else None
        sla = calculate_sla(assigned_at=assigned_at, now=utc_now(), temperature=temp, hot_started_at=hot_start)
        if sla.get("status") == "critical":
            return "superseded_by_breached"
    return None


# ---------------------------------------------------------------------------
# Process one alert — safe send order
# ---------------------------------------------------------------------------

async def process_one_alert(
    db=None, *, worker_id: str, sender: SlaAlertSender | None = None,
    pre_claimed: dict | None = None,
) -> dict:
    if db is None:
        db = get_async_db()

    # 1. Claim
    alert = pre_claimed or await claim_next_alert(db, worker_id=worker_id)
    if not alert:
        return {"status": "idle"}
    alert_id = alert["_id"]

    try:
        # 2. Revalidate
        reason = await _revalidate(db, alert)
        if reason:
            if reason == "superseded_by_breached":
                await cancel_alerts_for_cycle(db, assignment_cycle_id=alert["assignment_cycle_id"],
                                              reason=reason, except_level=alert.get("alert_level"))
            else:
                await cancel_alert(db, alert_id=alert_id, reason=reason)
            return {"status": "cancelled", "reason": reason}

        # 3. Mark delivery started (lease-gated: same _id, processing, same lease_owner, lease not expired)
        started = await mark_delivery_started(db, alert_id=alert_id, worker_id=worker_id)
        if not started:
            return {"status": "lost_lease", "reason": "delivery_start_not_acquired"}
        attempt_id = started.get("delivery_attempt_id")

        # 4. Call sender
        transport = get_sender(sender)
        phone = alert.get("recipient_phone_snapshot", "")
        message = alert.get("rendered_message", "")

        try:
            result: SenderResult = await asyncio.wait_for(
                transport(phone, message),
                timeout=PROVIDER_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await finalize_alert(db, alert_id=alert_id, state=ST_DELIVERY_UNCERTAIN,
                                 error="provider_timeout", delivery_outcome="timeout",
                                 delivery_attempt_id=attempt_id)
            return {"status": "delivery_uncertain", "reason": "timeout"}

        # 5. Finalize based on typed outcome
        if result.outcome == "confirmed_success" and result.provider_message_id:
            ok = await finalize_alert(db, alert_id=alert_id, state=ST_SENT,
                                      provider_message_id=result.provider_message_id,
                                      delivery_attempt_id=attempt_id)
            if not ok:
                # finalize failed → will be quarantined later, do NOT re-send
                await finalize_alert(db, alert_id=alert_id, state=ST_DELIVERY_UNCERTAIN,
                                     error="finalize_failed_after_confirmed_success",
                                     delivery_outcome="crash_after_success",
                                     delivery_attempt_id=attempt_id)
                return {"status": "delivery_uncertain", "reason": "crash_after_success"}
            return {"status": "sent", "provider_message_id": result.provider_message_id}

        elif result.outcome == "rejected_before_acceptance":
            await finalize_alert(db, alert_id=alert_id, state=ST_FAILED_RETRYABLE,
                                 error=result.error or "rejected", delivery_attempt_id=attempt_id)
            return {"status": "failed_retryable"}

        else:  # delivery_unknown
            await finalize_alert(db, alert_id=alert_id, state=ST_DELIVERY_UNCERTAIN,
                                 error=result.error, delivery_outcome=result.outcome,
                                 delivery_attempt_id=attempt_id)
            return {"status": "delivery_uncertain", "reason": result.outcome}

    except Exception as exc:
        try:
            doc = await db[COLLECTION].find_one({"_id": alert_id})
            if doc and doc.get("delivery_started_at"):
                await finalize_alert(db, alert_id=alert_id, state=ST_DELIVERY_UNCERTAIN,
                                     error=str(exc), delivery_outcome="crash_after_delivery_start",
                                     delivery_attempt_id=doc.get("delivery_attempt_id"))
                return {"status": "delivery_uncertain", "reason": "crash"}
        except Exception:
            pass
        try:
            await finalize_alert(db, alert_id=alert_id, state=ST_FAILED_RETRYABLE, error=str(exc))
        except Exception:
            logger.error("[SLA_ALERT] Could not finalize %s", alert_id)
        return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Batch entrypoint
# ---------------------------------------------------------------------------

async def process_alerts_batch(
    db=None, *, worker_id: str, sender: SlaAlertSender | None = None,
    max_total: int | None = None, max_per_recipient: int | None = None,
) -> dict:
    if not CRM_SLA_ALERTS_ENABLED:
        return {"status": "disabled", "processed": 0, "by_status": {},
                "reason": "CRM_SLA_ALERTS_ENABLED is false", "claims": 0, "sends": 0}

    if db is None:
        db = get_async_db()

    config = validate_live_send_config()
    if not config["valid"]:
        logger.error("[SLA_ALERT] %s", config["reason"])
        return {"status": "invalid_live_send_configuration", "processed": 0, "by_status": {},
                "reason": config["reason"]}

    max_total = max_total or MAX_PER_RUN
    max_per_recipient = max_per_recipient or MAX_PER_RECIPIENT_PER_RUN
    results: list[dict] = []
    recipient_counts: Counter = Counter()

    for _ in range(max_total):
        alert = await claim_next_alert(db, worker_id=worker_id)
        if not alert:
            break
        recipient = alert.get("recipient_user_id", "unknown")
        if recipient_counts[recipient] >= max_per_recipient:
            await db[COLLECTION].update_one(
                {"_id": alert["_id"], "state": ST_PROCESSING},
                {"$set": {"state": ST_PENDING, "lease_owner": None,
                           "lease_expires_at": None, "updated_at": utc_now()}},
            )
            continue
        recipient_counts[recipient] += 1
        results.append(await process_one_alert(db=db, worker_id=worker_id, sender=sender, pre_claimed=alert))

    return {"processed": len(results),
            "by_status": dict(Counter(r.get("status", "unknown") for r in results)),
            "recipients_hit": len(recipient_counts)}
