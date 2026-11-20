"""Credit first CRM SLA management only for a persisted, authorized human WA send.

The WhatsApp webhook and CRM send endpoint already authenticate the executive,
session, provider and active lead. Re-read the canonical message before using
the EXISTING MESSAGE_SENT_WAITING_RESPONSE management path; never infer a send
from a click, template, incoming customer text, automation or provider receipt
that was not saved. No new fields are added to `leads.messages`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

from .crm_management import record_management_result
from .crm_metrics import active_assignment_cycle, coerce_utc_datetime


HUMAN_SEND_SOURCES = ("crm_manual_api", "whatsapp_direct")


def credit_verified_outbound(db, *, lead_id, assignment_cycle_id: str,
                             actor_user_id: str, session_id: str,
                             provider_message_id: str) -> str:
    """Return credited/already_managed/unverified/stale; caller handles failures.

    Use one deterministic management key PER ASSIGNMENT CYCLE, not per
    message or provider echo. Concurrent/duplicate webhook deliveries should
    never create independent management records or follow-up reminders.
    """
    cycle_id = str(assignment_cycle_id or "")
    owner = str(actor_user_id or "")
    session = str(session_id or "")
    provider_id = str(provider_message_id or "")
    if not all((lead_id, cycle_id, owner, session, provider_id)):
        return "unverified"

    evidence = {
        "channel": "executive_whatsapp",
        "source": {"$in": list(HUMAN_SEND_SOURCES)},
        "role": "agent",
        "actor_type": "human_agent",
        "assigned_executive_id": owner,
        "assignment_cycle_id": cycle_id,
        "wasender_session_id": session,
        "$or": [{"provider_message_id": provider_id},
                {"crm_api_provider_message_id": provider_id}],
        "timestamp_source": {"$in": ["provider", "crm_api_accepted"]},
    }
    lead = db["leads"].find_one({
        "_id": lead_id,
        "lifecycle.assigned_to_user_id": owner,
        "lifecycle.current_assignment_cycle_id": cycle_id,
        "messages": {"$elemMatch": evidence},
    }, {
        "lifecycle": 1,
        "messages": {"$elemMatch": evidence},
    })
    if not lead or not lead.get("messages"):
        return "unverified"
    message = lead["messages"][0]
    occurred = coerce_utc_datetime(message.get("timestamp"))
    if not occurred or occurred > datetime.now(timezone.utc) + timedelta(minutes=2):
        return "unverified"

    cycle = active_assignment_cycle(db, lead_id)
    if not cycle or str(cycle.get("assignment_cycle_id") or "") != cycle_id:
        return "stale"
    if (str(cycle.get("assigned_to_user_id") or "") != owner
            or cycle.get("cycle_status") not in (None, "active")
            or cycle.get("unassigned_at") is not None):
        return "stale"
    assigned = coerce_utc_datetime(cycle.get("assigned_at"))
    if not assigned or occurred < assigned:
        return "stale"
    # The first-management SLA is already finished. Preserve any more
    # advanced recorded management result and never overwrite it with
    # a generic "waiting for reply" state.
    if (cycle.get("first_valid_management_at") is not None
            or cycle.get("sla_first_management_status") == "completed"):
        return "already_managed"

    identity = sha256(f"{lead_id}|{cycle_id}|{owner}".encode("utf-8")).hexdigest()
    result = record_management_result(
        db,
        lead_id=lead_id,
        assignment_cycle_id=cycle_id,
        actor_user_id=owner,
        result_type="MESSAGE_SENT_WAITING_RESPONSE",
        source="executive_whatsapp_verified",
        idempotency_key=f"executive_wa_first:{identity}",
        occurred_at=occurred,
        details_json={"notes": "WhatsApp enviado y guardado; pendiente respuesta"},
    )
    return "credited" if result.get("status") == "completed" else "unverified"
