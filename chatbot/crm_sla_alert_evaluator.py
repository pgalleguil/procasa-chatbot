"""CRM SLA Alert Evaluator — Phase 1, read-only dry-run.

Evaluates active assignment cycles against SLA thresholds.  Determines
which leads would receive a warning or breached alert, builds the exact
WhatsApp message text, and returns a structured report.

Guarantees:
- Zero MongoDB writes (no insert, update, find_one_and_update, create_index).
- Zero provider calls (no NotificationService, no whatsapp_client).
- Zero side effects on leads, cycles, events, or any other collection.
- Zero imports from webhook.py.
- Fully async MongoDB via Motor (no PyMongo sync calls on event loop).

Policy:
- MESSAGE_SENT_WAITING_RESPONSE does NOT stop the SLA (it's an outreach,
  not a valid management result).
- Valid results that DO stop the SLA: EFFECTIVE_CONTACT, CALL_NO_ANSWER,
  INVALID_NUMBER, FOLLOW_UP_REQUESTED, SCHEDULE_FOLLOW_UP, NOT_INTERESTED,
  DISCARDED_VALID_REASON.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from .constants import CHILE_TZ, BUSINESS_START_HOUR, BUSINESS_END_HOUR, BUSINESS_DAYS
from .crm_metrics import (
    INSTRUMENTATION_CUTOVER, calculate_sla, coerce_utc_datetime,
    event_evidence, utc_now,
)
from .crm_sla_alert_templates import (
    MESSAGE_DOMAIN, build_sla_message, build_lead_url, build_deadline_display,
    outreach_channel_label,
)
from .storage import get_async_db
from .utils import calculate_business_minutes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

THRESHOLD_WARNING_NORMAL = 150
THRESHOLD_BREACHED_NORMAL = 180
THRESHOLD_WARNING_HOT = 45
THRESHOLD_BREACHED_HOT = 60

ALERT_LEVEL_WARNING = "warning"
ALERT_LEVEL_BREACHED = "breached"

SLA_PROFILE_STANDARD = "standard"
SLA_PROFILE_HOT = "hot"

# ---------------------------------------------------------------------------
# Management results that DO stop the SLA
# ---------------------------------------------------------------------------

SLA_STOP_RESULTS = frozenset({
    "EFFECTIVE_CONTACT",
    "CALL_NO_ANSWER",
    "INVALID_NUMBER",
    "FOLLOW_UP_REQUESTED",
    "SCHEDULE_FOLLOW_UP",
    "NOT_INTERESTED",
    "DISCARDED_VALID_REASON",
})

# Management results that represent outreach (WhatsApp sent) but do NOT stop SLA
OUTREACH_RESULTS = frozenset({
    "MESSAGE_SENT_WAITING_RESPONSE",
    "EMAIL_SENT",
})

# ---------------------------------------------------------------------------
# Excluded stages / origins / synthetic phones
# ---------------------------------------------------------------------------

CLOSED_STAGES = frozenset({"ARCHIVED", "CLOSED_WON", "CLOSED_LOST"})
EXCLUDED_ORIGINS = frozenset({"test", "backfill", "reconciliation", "automated_historical"})
SYNTHETIC_PHONES = frozenset({"56900000000", "+56900000000", "0000000000"})

# ---------------------------------------------------------------------------
# Outreach classification (no DB writes, pure function)
# ---------------------------------------------------------------------------

_OUTREACH_PRIORITY = {
    "none": 0,
    "email_opened": 1, "phone_opened": 1,
    "whatsapp_opened": 2,
    "email_sent": 3,
    "whatsapp_sent": 4,
    "call_without_result": 5,
}


def classify_outreach_state(events, *, assigned_at=None, mgmt_results=None,
                            notifications=None) -> str:
    """Select strongest post-assignment outreach evidence.

    Events: CLICK_WHATSAPP_LEAD→whatsapp_opened, SEND_WA_LEAD(confirmed)→whatsapp_sent,
    CLICK_PHONE_LEAD→phone_opened, CALL_COMPLETED_LEAD(no result)→call_without_result.

    CALL_RESULT has never been observed in production and is excluded (fail-closed).
    call_without_result will only occur when phone integration is verified.

    mgmt_results: list of crm_management_results dicts.  If any have result_type
    MESSAGE_SENT_WAITING_RESPONSE (post-assignment), force whatsapp_sent.
    notifications: canonical sent CRM notifications for this assignment cycle.
    """
    start = coerce_utc_datetime(assigned_at) if assigned_at else None
    best, best_rank = "none", 0

    for event in events or ():
        occurred = coerce_utc_datetime(event.get("timestamp") or event.get("occurred_at"))
        if start and (not occurred or occurred < start):
            continue
        raw = str(event.get("type") or "").upper()
        meta = event.get("meta") or {}
        if raw == "CLICK_WHATSAPP_LEAD":
            state = "whatsapp_opened"
        elif raw == "SEND_WA_LEAD" and (
            event.get("confirmed") or meta.get("provider_message_id") or meta.get("sent") is True
        ):
            state = "whatsapp_sent"
        elif raw == "CLICK_PHONE_LEAD":
            state = "phone_opened"
        elif raw == "CLICK_EMAIL_LEAD":
            state = "email_opened"
        elif raw == "SEND_EMAIL_LEAD" and (
            event.get("confirmed") or meta.get("provider_message_id") or meta.get("sent") is True
        ):
            state = "email_sent"
        elif raw == "CALL_COMPLETED_LEAD" and not (
            event.get("result") or meta.get("result") or meta.get("contact_result")
        ):
            state = "call_without_result"
        else:
            continue
        rank = _OUTREACH_PRIORITY[state]
        if rank > best_rank:
            best, best_rank = state, rank

    # MESSAGE_SENT_WAITING_RESPONSE in management_results → whatsapp_sent
    for mr in mgmt_results or ():
        rt = str(mr.get("result_type") or "").upper()
        if rt in OUTREACH_RESULTS:
            occurred = coerce_utc_datetime(mr.get("occurred_at"))
            if start and occurred and occurred >= start:
                if _OUTREACH_PRIORITY["whatsapp_sent"] > best_rank:
                    best, best_rank = "whatsapp_sent", _OUTREACH_PRIORITY["whatsapp_sent"]

    # A legacy SEND_WA_LEAD event may be recorded without confirmation even
    # though the canonical assignment notification was accepted by WhatsApp.
    # The durable notification is authoritative for outreach classification.
    if any(
        str(n.get("state") or "").lower() == "sent"
        and n.get("provider_message_id")
        for n in notifications or ()
    ) and _OUTREACH_PRIORITY["whatsapp_sent"] > best_rank:
        best, best_rank = "whatsapp_sent", _OUTREACH_PRIORITY["whatsapp_sent"]

    return best


# ---------------------------------------------------------------------------
# Business minutes arithmetic
# ---------------------------------------------------------------------------

def add_business_minutes(start_dt: datetime, minutes_to_add: float) -> datetime:
    if start_dt.tzinfo is None:
        start_dt = CHILE_TZ.localize(start_dt)
    start_local = start_dt.astimezone(CHILE_TZ)
    remaining = float(minutes_to_add)
    current = start_local

    day_start = current.replace(hour=BUSINESS_START_HOUR, minute=0, second=0, microsecond=0)
    day_end = current.replace(hour=BUSINESS_END_HOUR, minute=0, second=0, microsecond=0)
    if current < day_start:
        current = day_start
    elif current >= day_end:
        current = day_start + timedelta(days=1)
        while current.weekday() not in BUSINESS_DAYS:
            current += timedelta(days=1)

    while remaining > 0:
        if current.weekday() in BUSINESS_DAYS:
            day_end = current.replace(hour=BUSINESS_END_HOUR, minute=0, second=0, microsecond=0)
            available = (day_end - current).total_seconds() / 60.0
            if available >= remaining:
                result = current + timedelta(minutes=remaining)
                return result.astimezone(timezone.utc)
            remaining -= available
        current = current.replace(hour=BUSINESS_START_HOUR, minute=0, second=0, microsecond=0) + timedelta(days=1)
        while current.weekday() not in BUSINESS_DAYS:
            current += timedelta(days=1)
    return current.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lead_first_name(lead: dict) -> str:
    prospecto = lead.get("prospecto") or {}
    name = str(prospecto.get("nombre") or "Cliente").strip()
    return name.split()[0] if name and name != "Cliente" else name


def _lead_property_code(lead: dict) -> str:
    prospecto = lead.get("prospecto") or {}
    return str(prospecto.get("codigo") or lead.get("property_code") or "S/N")


def _is_test_lead(lead: dict) -> bool:
    phone = str(lead.get("phone") or "")
    if phone in SYNTHETIC_PHONES:
        return True
    origin = str(lead.get("lead_origin") or lead.get("origin", "")).lower()
    if origin in EXCLUDED_ORIGINS:
        return True
    prospecto = lead.get("prospecto") or {}
    source = str(prospecto.get("origen", "")).lower()
    if source in EXCLUDED_ORIGINS:
        return True
    return False


# ---------------------------------------------------------------------------
# Async executive phone lookup (Motor, no PyMongo sync on event loop)
# ---------------------------------------------------------------------------

async def _get_executive_phone_async(db, executive_name: str) -> str | None:
    if not executive_name:
        return None
    user = await db["usuarios"].find_one(
        {"nombre": executive_name, "is_active": True, "rol": "agente"},
        {"telefono": 1, "tel": 1, "movil": 1},
    )
    if not user:
        return None
    phone = user.get("telefono") or user.get("tel") or user.get("movil")
    return str(phone).strip() if phone else None


async def _get_assigned_agent_async(db, user_id: str) -> dict | None:
    """Resolve the current recipient strictly from an active agent record."""
    if not user_id:
        return None
    query = {"_id": user_id, "is_active": True, "rol": "agente"}
    user = await db["usuarios"].find_one(
        query,
        {"_id": 1, "nombre": 1, "telefono": 1, "tel": 1, "movil": 1},
    )
    if not user:
        try:
            query["_id"] = ObjectId(user_id)
            user = await db["usuarios"].find_one(
                query,
                {"_id": 1, "nombre": 1, "telefono": 1, "tel": 1, "movil": 1},
            )
        except Exception:
            user = None
    if user:
        return user
    return None


# ---------------------------------------------------------------------------
# Main evaluation (async, read-only, fully async MongoDB)
# ---------------------------------------------------------------------------

async def evaluate_sla_alerts(
    db=None,
    *,
    limit_cycles: int = 2000,
    limit_leads: int = 2000,
    limit_events: int = 500,
    limit_mgmt_results: int = 500,
    alert_cutover=None,
    now=None,
) -> dict:
    if db is None:
        db = get_async_db()

    now_utc = now or utc_now()
    mongo_cutover = coerce_utc_datetime(INSTRUMENTATION_CUTOVER)

    if alert_cutover is None:
        import chatbot.crm_sla_alert_settings as _s
        alert_cutover = _s.CUTOVER_AT
    if alert_cutover is None:
        return {
            "as_of": now_utc.isoformat(), "total_cycles_evaluated": 0,
            "included": 0, "excluded_by_reason": {"missing_alert_cutover": 0},
            "alerts": [], "provider_calls": 0, "writes": 0,
            "dry_run": True, "alert_cutover_used": None,
        }

    # 1. Fetch active cycles
    cycles_cursor = db["crm_assignment_cycles"].find({
        "unassigned_at": None, "cycle_status": "active",
        "assigned_at": {"$gte": mongo_cutover},
        "assignment_cycle_id": {"$exists": True},
    })
    cycles = await cycles_cursor.to_list(length=limit_cycles)
    if not cycles:
        return {
            "as_of": now_utc.isoformat(), "total_cycles_evaluated": 0,
            "included": 0, "excluded_by_reason": {},
            "alerts": [], "provider_calls": 0, "writes": 0,
            "dry_run": True, "alert_cutover_used": alert_cutover.isoformat(),
        }

    # 2. Fetch leads
    lead_ids = [c["lead_id"] for c in cycles]
    leads_cursor = db["leads"].find(
        {"_id": {"$in": lead_ids}},
        {"messages": 0, "stage_history": 0},
    )
    leads = await leads_cursor.to_list(length=limit_leads)
    lead_map = {str(ld["_id"]): ld for ld in leads if ld.get("_id") is not None}

    # 3. Pre-fetch management_results for all active cycles
    cycle_ids = [str(c.get("assignment_cycle_id", "")) for c in cycles if c.get("assignment_cycle_id")]
    mgmt_cursor = db["crm_management_results"].find(
        {"assignment_cycle_id": {"$in": cycle_ids}},
    )
    all_mgmt = await mgmt_cursor.to_list(length=limit_mgmt_results)
    mgmt_by_cycle: dict[str, list[dict]] = {}
    for m in all_mgmt:
        cid = str(m.get("assignment_cycle_id", ""))
        mgmt_by_cycle.setdefault(cid, []).append(m)

    notification_cursor = db["crm_notifications_v1"].find(
        {"metadata.assignment_cycle_id": {"$in": cycle_ids}, "state": "sent",
         "provider_message_id": {"$exists": True}},
        {"metadata.assignment_cycle_id": 1, "state": 1,
         "provider_message_id": 1},
    )
    sent_notifications = await notification_cursor.to_list(length=limit_mgmt_results)
    notifications_by_cycle: dict[str, list[dict]] = {}
    for n in sent_notifications:
        metadata = n.get("metadata") or {}
        cid = str(metadata.get("assignment_cycle_id") or "")
        if cid:
            notifications_by_cycle.setdefault(cid, []).append(n)

    # 4. Evaluate each cycle
    excluded = Counter()
    alerts: list[dict] = []
    seen_dedup: set[str] = set()

    for cycle in cycles:
        lid = cycle.get("lead_id")
        if lid is None:
            excluded["missing_lead_id"] += 1; continue

        lead = lead_map.get(str(lid))
        if lead is None:
            excluded["lead_not_found"] += 1; continue

        cycle_id = str(cycle.get("assignment_cycle_id", ""))
        if not cycle_id:
            excluded["missing_assignment_cycle_id"] += 1; continue

        assigned_at = coerce_utc_datetime(cycle.get("assigned_at"))
        if not assigned_at:
            excluded["missing_assigned_at"] += 1; continue
        sla_started_at = coerce_utc_datetime(cycle.get("sla_started_at")) or assigned_at

        recipient_user_id = str(cycle.get("assigned_to_user_id") or "")
        if not recipient_user_id:
            excluded["missing_assigned_to_user_id"] += 1; continue

        # ---- Cutover ----
        if sla_started_at < alert_cutover:
            excluded["before_alert_cutover"] += 1; continue
        if sla_started_at < mongo_cutover:
            excluded["pre_operational_cutover"] += 1; continue

        # ---- Test / synthetic ----
        if _is_test_lead(lead):
            excluded["test_or_synthetic"] += 1; continue

        # ---- Closed ----
        stage = str(lead.get("pipeline_stage") or lead.get("stage", "")).upper()
        if stage in CLOSED_STAGES:
            excluded["lead_closed"] += 1; continue

        # ---- Excluded origins ----
        reason = str(cycle.get("reason") or "").lower()
        if reason in EXCLUDED_ORIGINS:
            excluded[f"excluded_origin:{reason}"] += 1; continue
        cycle_origin = str(cycle.get("cycle_origin") or "").lower()
        if cycle_origin in EXCLUDED_ORIGINS:
            excluded[f"excluded_cycle_origin:{cycle_origin}"] += 1; continue

        # ---- Query crm_management_results for this cycle ----
        cycle_mgmts = mgmt_by_cycle.get(cycle_id, [])

        # Check for valid SLA-stop results
        has_valid_result = any(
            str(m.get("result_type") or "").upper() in SLA_STOP_RESULTS
            and coerce_utc_datetime(m.get("occurred_at"))
            and coerce_utc_datetime(m.get("occurred_at")) >= assigned_at
            for m in cycle_mgmts
        )
        if has_valid_result:
            excluded["has_valid_management"] += 1; continue

        # ---- Event evidence (GESTION_LOG / HUMAN_NOTE / MANUAL_ENTRY) ----
        events_cursor = db["crm_events"].find({
            "lead_id": lid, "timestamp": {"$gte": assigned_at},
        })
        events = await events_cursor.to_list(length=limit_events)
        if any(event_evidence(e)["management"] for e in events):
            excluded["has_valid_management"] += 1; continue

        # ---- Temperature and hot_since ----
        temp = str(lead.get("lead_temperature_effective") or "").upper()
        is_hot = temp == "HOT"
        hot_start = None
        if is_hot:
            lifecycle = lead.get("lifecycle") or {}
            hs = lifecycle.get("hot_since")
            hot_start = coerce_utc_datetime(hs) if hs else None
            if hot_start and hot_start < sla_started_at:
                hot_start = sla_started_at

        # ---- Classify outreach (uses events + management_results) ----
        outreach_state = classify_outreach_state(
            events, assigned_at=assigned_at, mgmt_results=cycle_mgmts,
            notifications=notifications_by_cycle.get(cycle_id, []),
        )

        # ---- Calculate SLA ----
        sla = calculate_sla(
            assigned_at=sla_started_at, now=now_utc,
            temperature=temp, hot_started_at=hot_start,
        )
        sla_status = sla.get("status", "good")
        if sla_status not in {"near_critical", "critical"}:
            excluded["below_threshold"] += 1; continue

        # ---- Alert level and profile ----
        alert_level = ALERT_LEVEL_BREACHED if sla_status == "critical" else ALERT_LEVEL_WARNING
        sla_profile = SLA_PROFILE_HOT if is_hot else SLA_PROFILE_STANDARD

        # ---- Elapsed ----
        effective_start = hot_start if (is_hot and hot_start) else sla_started_at
        elapsed = int(max(0, sla.get("hot_minutes" if is_hot else "minutes", 0) or 0))

        # ---- Deadline ----
        deadline_threshold = THRESHOLD_BREACHED_HOT if is_hot else THRESHOLD_BREACHED_NORMAL
        deadline_dt = add_business_minutes(effective_start, deadline_threshold)
        deadline_display = build_deadline_display(deadline_dt, CHILE_TZ)

        # ---- Reassignment fields (always disabled in this phase) ----
        sla_breached_at = deadline_dt.isoformat() if alert_level == ALERT_LEVEL_BREACHED else None
        reassignment_grace_expires_at = None

        # ---- Dedup ----
        dedup_key = f"{cycle_id}|{alert_level}|{recipient_user_id}"
        if dedup_key in seen_dedup:
            excluded["duplicate_candidate"] += 1; continue
        seen_dedup.add(dedup_key)

        # ---- Executive phone (async Motor) ----
        executive_fallback = str(lead.get("ejecutivo_asignado") or "")
        recipient_user = await _get_assigned_agent_async(
            db, recipient_user_id,
        )
        if not recipient_user:
            excluded["recipient_not_active_agent"] += 1
            continue
        executive = str(recipient_user.get("nombre") or executive_fallback)
        executive_phone = (
            recipient_user.get("telefono")
            or recipient_user.get("tel")
            or recipient_user.get("movil")
        )
        executive_phone = str(executive_phone).strip() if executive_phone else None

        # ---- Build message ----
        client_name = _lead_first_name(lead)
        property_code = _lead_property_code(lead)
        lead_url = build_lead_url(lead)
        message = build_sla_message(
            hot=is_hot, breached=(alert_level == ALERT_LEVEL_BREACHED),
            client_first_name=client_name, property_code=property_code,
            elapsed_minutes=elapsed, deadline_display=deadline_display,
            lead_url=lead_url, outreach_state=outreach_state,
        )

        alerts.append({
            "message_domain": MESSAGE_DOMAIN,
            "assignment_cycle_id": cycle_id,
            "lead_id": str(lid),
            "recipient_user_id": recipient_user_id,
            "alert_level": alert_level,
            "sla_profile": sla_profile,
            "elapsed_business_minutes": elapsed,
            "deadline_at": deadline_dt.isoformat() if deadline_dt else None,
            "deadline_dt": deadline_dt,  # timezone-aware UTC datetime for repository
            "deadline_display": deadline_display,
            "outreach_state": outreach_state,
            "outreach_channel": outreach_channel_label(outreach_state),
            "executive_name": executive,
            "executive_phone": executive_phone,
            "client_first_name": client_name,
            "property_code": property_code,
            "lead_url": lead_url,
            "message": message,
            "idempotency_dedup_key": dedup_key,
            "sla_breached_at": sla_breached_at,
            "reassignment_grace_expires_at": reassignment_grace_expires_at,
            "reassignment_policy_version": "crm_sla_reassignment_v1",
            "reassignment_state": "disabled",
            "has_valid_management": False,
            "cycle_active": True,
            "lead_closed": False,
            "executive_current": executive,
        })

    return {
        "as_of": now_utc.isoformat(),
        "alert_cutover_used": alert_cutover.isoformat(),
        "total_cycles_evaluated": len(cycles),
        "included": len(alerts),
        "excluded_by_reason": dict(excluded),
        "alerts": alerts,
        "provider_calls": 0,
        "writes": 0,
        "dry_run": True,
    }
