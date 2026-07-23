"""Canonical CRM measurement rules.

This module is the only place where CRM human management, contact evidence,
assignment cycles and SLA are defined.  It deliberately contains no weekly
report or WhatsApp concerns.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional
import re
import uuid

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .constants import CHILE_TZ
from .utils import calculate_business_minutes

METRIC_VERSION = "crm_metrics_v1"
INSTRUMENTATION_CUTOVER = "2026-07-20T00:00:00-04:00"
MANAGEMENT_ENFORCEMENT_CUTOVER = "2026-07-23T22:00:00Z"  # Phase 2 deploy; override via env

OPEN_ONLY_EVENT_TYPES = frozenset({
    "CLICK_WHATSAPP_LEAD", "CLICK_PHONE_LEAD", "CLICK_EMAIL_LEAD",
    "CLICK_WHATSAPP_OWNER", "CLICK_PHONE_OWNER", "CLICK_EMAIL_OWNER",
    "SEND_WA_LEAD", "SEND_EMAIL_LEAD", "CALL_COMPLETED_LEAD",
    "OPEN_DETAIL", "NAVIGATION", "FILTER", "ASSIGNMENT", "assignment",
    "ALERT", "ALERT_SENT", "alert_sent", "BOT_MSG",
})
VALID_MANAGEMENT_EVENT_TYPES = frozenset({
    "GESTION_LOG", "HUMAN_NOTE", "CONTACT_RESULT", "STATUS_CHANGE",
    "MANUAL_ENTRY",
})
REGISTERED_OUTREACH_EVENT_TYPES = frozenset({
    "CLICK_WHATSAPP_LEAD", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", "CALL_COMPLETED_LEAD",
})
CONTACT_ATTEMPT_RESULTS = frozenset({
    "NO_RESPONDIO", "OCUPADO", "NUMERO_INVALIDO", "MENSAJE_ENVIADO",
    "CONTACTADO", "SOLICITA_SEGUIMIENTO", "NO_INTERESADO", "OTRO",
    "MESSAGE_SENT_WAITING_RESPONSE", "CALL_NO_ANSWER", "EMAIL_SENT",
    "EFFECTIVE_CONTACT", "FOLLOW_UP_REQUESTED", "INVALID_NUMBER",
})
EFFECTIVE_CONTACT_RESULTS = frozenset({
    "CONTACTADO", "SOLICITA_SEGUIMIENTO", "NO_INTERESADO",
    "EFFECTIVE_CONTACT", "FOLLOW_UP_REQUESTED",
})
HUMAN_ACTOR_TYPES = frozenset({"human", "agent", "administrator", "supervisor"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def coerce_utc_datetime(value: Any, *, naive_timezone=CHILE_TZ) -> Optional[datetime]:
    """Read BSON datetimes and legacy ISO values without silently guessing invalid data."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = naive_timezone.localize(parsed)
    return parsed.astimezone(timezone.utc)


def normalize_phone(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


@dataclass(frozen=True)
class LeadResolution:
    lead: Optional[Mapping[str, Any]]
    status: str
    candidate_count: int


def resolve_canonical_lead(db, *, lead_id=None, phone=None) -> LeadResolution:
    """Resolve by lead._id; phone fallback is accepted only when unambiguous."""
    if lead_id is not None:
        lead = db["leads"].find_one({"_id": lead_id})
        return LeadResolution(lead, "resolved" if lead else "not_found", int(bool(lead)))
    normalized = normalize_phone(phone)
    if not normalized:
        return LeadResolution(None, "missing_identity", 0)
    candidates = list(db["leads"].find(
        {"phone": {"$regex": rf"^\+?{re.escape(normalized)}$"}}, limit=2
    ))
    if len(candidates) == 1:
        return LeadResolution(candidates[0], "resolved_legacy_phone", 1)
    return LeadResolution(None, "ambiguous_phone" if candidates else "not_found", len(candidates))


def normalize_result(value: Any) -> Optional[str]:
    if value is None:
        return None
    result = re.sub(r"[^A-Z0-9]+", "_", str(value).strip().upper()).strip("_")
    aliases = {
        "INTENTO_FALLIDO": "NO_RESPONDIO", "HABLE": "CONTACTADO",
        "REQUIERE_SEGUIMIENTO": "SOLICITA_SEGUIMIENTO",
        "LEAD_PAUSADO": "SOLICITA_SEGUIMIENTO", "LEAD_CERRADO": "NO_INTERESADO",
        "WHATSAPP_ENVIADO": "MENSAJE_ENVIADO", "VISITA_AGENDADA": "CONTACTADO",
        "MENSAJE_ENVIADO_ESPERANDO_RESPUESTA": "MESSAGE_SENT_WAITING_RESPONSE",
        "LLAMADA_SIN_RESPUESTA": "CALL_NO_ANSWER",
        "CONTACTO_EFECTIVO": "EFFECTIVE_CONTACT",
        "NUMERO_INVALIDO": "INVALID_NUMBER",
    }
    return aliases.get(result, result) if result else None


def event_evidence(event: Mapping[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "").upper()
    meta = event.get("meta") or {}
    actor = event.get("actor")
    actor_type = str(event.get("actor_type") or meta.get("actor_type") or "").lower()
    human = bool(actor) and str(actor).lower() not in {"system", "bot", "sistema", "none"}
    human = human and (not actor_type or actor_type in HUMAN_ACTOR_TYPES)
    result = normalize_result(event.get("result") or meta.get("result") or meta.get("contact_result"))
    confirmed = bool(event.get("confirmed", meta.get("confirmed", False)))
    identifiable = event.get("lead_id") is not None
    attempt = human and identifiable and confirmed and result in CONTACT_ATTEMPT_RESULTS
    effective = attempt and result in EFFECTIVE_CONTACT_RESULTS
    meaningful_change = bool(meta.get("meaningful_change"))
    management = human and identifiable and event_type not in OPEN_ONLY_EVENT_TYPES and (
        event_type in VALID_MANAGEMENT_EVENT_TYPES
        and event_type != "CONTACT_RESULT"
        and (result is not None or meaningful_change)
    )
    return {
        "human": human, "management": management, "contact_attempt": attempt,
        "effective_contact": effective, "result": result,
    }


def registered_outreach_evidence(event: Optional[Mapping[str, Any]], *, assigned_at=None,
                                 assignment_cycle_id=None,
                                 allow_historical_for_presentation=False) -> dict[str, Any]:
    """Interpret a recorded outreach event for presentation only — never counts as management."""
    if not event or str(event.get("type") or "").upper() not in REGISTERED_OUTREACH_EVENT_TYPES:
        return {"recognized": False, "occurred_at": None, "reason": "not_registered_outreach"}
    occurred = coerce_utc_datetime(event.get("timestamp") or event.get("occurred_at"))
    assigned = coerce_utc_datetime(assigned_at)
    if not occurred:
        return {"recognized": False, "occurred_at": None, "reason": "invalid_timestamp"}
    if assigned and occurred < assigned and not allow_historical_for_presentation:
        return {"recognized": False, "occurred_at": occurred, "reason": "previous_assignment_cycle"}
    event_cycle = event.get("assignment_cycle_id")
    if (assignment_cycle_id and event_cycle and str(event_cycle) != str(assignment_cycle_id)
            and not allow_historical_for_presentation):
        return {"recognized": False, "occurred_at": occurred, "reason": "different_assignment_cycle"}
    return {"recognized": True, "occurred_at": occurred, "reason": "registered_outreach"}


def unique_managed_lead_ids(events: Iterable[Mapping[str, Any]]) -> set[Any]:
    return {event["lead_id"] for event in events if event_evidence(event)["management"]}


def create_assignment_cycle(db, *, lead, assigned_to_user_id, assigned_by,
                            reason, assigned_at=None, assigned_to_display_name=None) -> dict[str, Any]:
    assigned_at = coerce_utc_datetime(assigned_at) or utc_now()
    active = db["crm_assignment_cycles"].find_one({"lead_id": lead["_id"], "unassigned_at": None})
    if (active and active.get("schema_version") == "crm_assignment_cycle_v1"
            and active.get("cycle_status") == "active"
            and str(active.get("assigned_to_user_id")) == str(assigned_to_user_id)):
        return active
    if active:
        db["crm_assignment_cycles"].update_one(
            {"_id": active["_id"], "unassigned_at": None},
            {"$set": {"unassigned_at": assigned_at, "cycle_status": "closed"}},
        )
    cycle = {
        "assignment_cycle_id": str(uuid.uuid4()), "lead_id": lead["_id"],
        "assigned_to_user_id": assigned_to_user_id, "assigned_at": assigned_at,
        "assigned_to_display_name": assigned_to_display_name or str(assigned_to_user_id),
        "unassigned_at": None, "assigned_by": assigned_by, "reason": reason,
        "metric_version": METRIC_VERSION, "schema_version": "crm_assignment_cycle_v1",
        "cycle_status": "active",
        "applied_transition_ids": [],
    }
    try:
        db["crm_assignment_cycles"].insert_one(cycle)
    except DuplicateKeyError:
        # The partial unique active-cycle index resolves concurrent retries.
        winner = active_assignment_cycle(db, lead["_id"])
        if winner and str(winner.get("assigned_to_user_id")) == str(assigned_to_user_id):
            return winner
        raise
    return cycle


def audit_and_ensure_assignment_cycle_indexes(db, *, create=False) -> dict[str, Any]:
    """Dry-run by default; legacy cycles are outside the partial indexes."""
    collection = db["crm_assignment_cycles"]
    canonical_filter = {
        "schema_version": "crm_assignment_cycle_v1", "cycle_status": "active",
        "lead_id": {"$exists": True}, "assigned_to_user_id": {"$exists": True},
        "assignment_cycle_id": {"$exists": True},
    }
    eligible = collection.count_documents(canonical_filter)
    duplicates = list(collection.aggregate([
        {"$match": canonical_filter},
        {"$group": {"_id": "$lead_id", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
    ]))
    created = []
    if create and not duplicates:
        created.append(collection.create_index(
            [("lead_id", 1), ("cycle_status", 1)], unique=True,
            partialFilterExpression=canonical_filter, name="uq_crm_active_cycle_v1",
        ))
        created.append(collection.create_index(
            [("lead_id", 1), ("assigned_to_user_id", 1), ("cycle_status", 1), ("assignment_cycle_id", 1)],
            unique=True, partialFilterExpression=canonical_filter, name="uq_crm_cycle_identity_v1",
        ))
    return {"eligible_documents": eligible, "duplicates": duplicates,
            "safe_to_create": not duplicates, "created": created}


def active_assignment_cycle(db, lead_id):
    return db["crm_assignment_cycles"].find_one(
        {"lead_id": lead_id, "unassigned_at": None}, sort=[("assigned_at", -1)]
    )


def calculate_sla(*, assigned_at, first_valid_management_at=None, now=None) -> dict[str, Any]:
    """Single SLA: assignment start -> first valid human management."""
    start = coerce_utc_datetime(assigned_at)
    end = coerce_utc_datetime(first_valid_management_at)
    current = coerce_utc_datetime(now) if now is not None else utc_now()
    if not start:
        return {"status": "unknown", "minutes": None, "fulfilled": False}
    boundary = end or current
    minutes = max(0, calculate_business_minutes(start.astimezone(CHILE_TZ), boundary.astimezone(CHILE_TZ)))
    if end:
        status = "fulfilled"
    elif minutes >= 180:
        status = "critical"
    elif minutes >= 150:
        status = "near_critical"
    elif minutes >= 60:
        status = "warning"
    else:
        status = "good"
    return {"status": status, "minutes": minutes, "fulfilled": bool(end), "age_minutes": minutes}


def is_pre_cutover_cycle(assigned_at, *, cutover=None) -> bool:
    """Return True if the cycle's assigned_at predates the management enforcement cutover.

    Pre-cutover cycles are exempt from new SLA policy. They are displayed as
    "Histórico" and excluded from compliance metrics, digest, alerts, and escalations.
    """
    assigned = coerce_utc_datetime(assigned_at)
    if not assigned:
        return False
    if cutover is not None:
        cutoff = coerce_utc_datetime(cutover)
    else:
        from config import Config
        raw = getattr(Config, "CRM_MANAGEMENT_ENFORCEMENT_CUTOVER_AT", None) or MANAGEMENT_ENFORCEMENT_CUTOVER
        cutoff = coerce_utc_datetime(raw)
    return assigned < cutoff


def atomic_transition_to_hot(db, *, cycle_id, notification_id, timestamp):
    """Atomically close NON_HOT and open HOT for a cycle.

    Uses ``applied_transition_ids`` for idempotency.  The operation is a
    single ``find_one_and_update`` that:
    1. Verifies the cycle is active.
    2. Confirms the transition ID has not been applied before.
    3. Confirms a NON_HOT segment is active (segment_end is null).
    4. Confirms NO HOT segment is already active.
    5. Closes the NON_HOT segment and opens HOT in one pipeline.

    Returns the updated cycle or ``None`` if the transition was already applied
    or the preconditions were not met.
    """
    transition_id = f"{cycle_id}|{notification_id}|NON_HOT_to_HOT"
    now = coerce_utc_datetime(timestamp) or utc_now()
    notification_id_str = str(notification_id)

    pipeline = [
        {"$set": {
            "sla_segments": {
                "$concatArrays": [
                    {
                        "$map": {
                            "input": {"$ifNull": ["$sla_segments", []]},
                            "as": "seg",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$eq": ["$$seg.policy", "NON_HOT"]},
                                            {"$eq": ["$$seg.segment_end", None]},
                                        ]
                                    },
                                    {
                                        "$mergeObjects": [
                                            "$$seg",
                                            {
                                                "segment_end": now,
                                                "end_reason": "superseded_by_hot",
                                            },
                                        ]
                                    },
                                    "$$seg",
                                ]
                            },
                        }
                    },
                    [
                        {
                            "policy": "HOT",
                            "segment_start": now,
                            "segment_end": None,
                            "end_reason": None,
                            "notification_id": notification_id_str,
                        }
                    ],
                ]
            },
            "applied_transition_ids": {
                "$setUnion": [
                    {"$ifNull": ["$applied_transition_ids", []]},
                    [transition_id],
                ]
            },
        }}
    ]

    return db["crm_assignment_cycles"].find_one_and_update(
        {
            "assignment_cycle_id": cycle_id,
            "cycle_status": "active",
            "applied_transition_ids": {"$ne": transition_id},
            "$and": [
                {"sla_segments": {"$elemMatch": {"policy": "NON_HOT", "segment_end": None}}},
                {"sla_segments": {"$not": {"$elemMatch": {"policy": "HOT", "segment_end": None}}}},
            ],
        },
        pipeline,
        return_document=ReturnDocument.AFTER,
    )


def pipeline_activity_in_period(*, visits, events, start, end) -> dict[str, int]:
    start_utc, end_utc = coerce_utc_datetime(start), coerce_utc_datetime(end)
    def inside(value):
        parsed = coerce_utc_datetime(value)
        return bool(parsed and start_utc <= parsed < end_utc)
    visit_ids = {
        str(v.get("lead_id") or v.get("_id")) for v in visits
        if inside(v.get("confirmed_at") or v.get("created_at"))
    }
    won, lost = set(), set()
    for event in events:
        if not inside(event.get("timestamp")):
            continue
        result = normalize_result((event.get("meta") or {}).get("to") or event.get("result"))
        lead_id = event.get("lead_id")
        if lead_id is None:
            continue
        if result == "CLOSED_WON": won.add(lead_id)
        elif result == "CLOSED_LOST": lost.add(lead_id)
    return {"visits": len(visit_ids), "closed_won": len(won), "closed_lost": len(lost)}


def build_snapshot_document(*, period_start, period_end, cohort, pipeline,
                            priorities, executives, data_quality) -> dict[str, Any]:
    return {
        "period": {"start": period_start, "end": period_end, "timezone": "America/Santiago"},
        "cohort": cohort, "pipeline_activity": pipeline, "current_priorities": priorities,
        "executives": executives, "metric_version": METRIC_VERSION,
        "data_quality": data_quality, "generated_at": utc_now(), "immutable": True,
    }


def validate_list_parity(*, kpis: Mapping[str, int], listed_total: int,
                         state_filter: Optional[str] = None) -> dict[str, Any]:
    """Invariant shared by cards, state panel, list and pagination."""
    key_by_filter = {
        None: "scope_total", "Todos": "scope_total", "NEW": "nuevo", "nuevo": "nuevo",
        "GRUPO_GESTION": "gestion", "GRUPO_VISITA": "visita", "GRUPO_CERRADO": "cerrado",
    }
    key = key_by_filter.get(state_filter)
    if not key:
        return {"validated": True, "metric": None}
    expected = int(kpis.get(key, 0))
    return {"validated": expected == int(listed_total), "metric": key,
            "expected": expected, "listed_total": int(listed_total)}


def persist_immutable_snapshot(db, snapshot: Mapping[str, Any]):
    """Explicit preparation hook; it does not schedule or publish anything."""
    document = dict(snapshot)
    if document.get("metric_version") != METRIC_VERSION:
        raise ValueError("snapshot metric_version mismatch")
    period = document.get("period") or {}
    key = f"{METRIC_VERSION}:{period.get('start')}:{period.get('end')}"
    existing = db["crm_metric_snapshots"].find_one({"snapshot_key": key})
    if existing:
        return existing
    document["snapshot_key"] = key
    db["crm_metric_snapshots"].insert_one(document)
    return document


def chile_period_bounds(period_start, period_end):
    """Inclusive local dates represented as an exclusive UTC interval."""
    start_date = period_start if hasattr(period_start, "year") and not isinstance(period_start, str) else datetime.fromisoformat(str(period_start)).date()
    end_date = period_end if hasattr(period_end, "year") and not isinstance(period_end, str) else datetime.fromisoformat(str(period_end)).date()
    start_local = CHILE_TZ.localize(datetime.combine(start_date, datetime.min.time()))
    end_local = CHILE_TZ.localize(datetime.combine(end_date, datetime.min.time()))
    return start_local.astimezone(timezone.utc), (end_local + __import__("datetime").timedelta(days=1)).astimezone(timezone.utc)


def in_utc_interval(value, start_utc, end_utc) -> bool:
    parsed = coerce_utc_datetime(value)
    return bool(parsed and start_utc <= parsed < end_utc)


def historical_temperature_at(lead: Mapping[str, Any], cutoff_utc) -> Optional[str]:
    """Return HOT/COLD only from timestamped evidence, never from current inventory."""
    evidence = []
    for entry in lead.get("temperature_history") or []:
        at = coerce_utc_datetime(entry.get("at") or entry.get("timestamp"))
        value = str(entry.get("value") or entry.get("temperature") or "").upper()
        if at and at < cutoff_utc and value in {"HOT", "COLD"}:
            evidence.append((at, value))
    return max(evidence, default=(None, None), key=lambda item: item[0])[1]


def format_business_age(minutes: Optional[int]) -> Optional[str]:
    if minutes is None:
        return None
    minutes = max(0, int(minutes))
    days, remaining = divmod(minutes, 8 * 60)
    hours = remaining // 60
    if days and hours:
        return f"{days} d\u00eda{'s' if days != 1 else ''} h\u00e1bil{'es' if days != 1 else ''} y {hours} hora{'s' if hours != 1 else ''}"
    if days:
        return f"{days} d\u00eda{'s' if days != 1 else ''} h\u00e1bil{'es' if days != 1 else ''}"
    return f"{hours} hora{'s' if hours != 1 else ''} h\u00e1bil{'es' if hours != 1 else ''}"


def build_weekly_crm_snapshot(db, *, period_start, period_end, priority_as_of,
                              executive_order: Iterable[str]) -> dict[str, Any]:
    """Build cohort, pipeline and Monday inventory from canonical CRM evidence."""
    start_utc, end_utc = chile_period_bounds(period_start, period_end)
    priority_utc = coerce_utc_datetime(priority_as_of)
    if not priority_utc:
        raise ValueError("priority_as_of invÃ¡lido")
    cutover_utc = coerce_utc_datetime(INSTRUMENTATION_CUTOVER)
    if start_utc < cutover_utc:
        raise ValueError("El periodo es anterior al corte operativo de CRM")

    leads = list(db["leads"].find({}, {"messages": 0}))
    lead_by_id = {lead["_id"]: lead for lead in leads}
    cohort_ids = {
        lead["_id"] for lead in leads
        if in_utc_interval(lead.get("created_at") or (lead.get("lifecycle") or {}).get("created_at"), start_utc, end_utc)
    }
    events = list(db["crm_events"].find({}))
    period_events = [event for event in events if in_utc_interval(event.get("timestamp"), start_utc, end_utc)]
    valid_management = [event for event in period_events if event_evidence(event)["management"]]
    managed_ids = {event["lead_id"] for event in valid_management if event.get("lead_id") in cohort_ids}
    unmanaged_ids = cohort_ids - managed_ids

    attempts = {e["lead_id"] for e in period_events if event_evidence(e)["contact_attempt"] and e.get("lead_id") in lead_by_id}
    effective = {e["lead_id"] for e in period_events if event_evidence(e)["effective_contact"] and e.get("lead_id") in lead_by_id}
    ambiguous = sum(1 for e in period_events if e.get("identity_status") == "ambiguous_phone")
    unresolved_actor = sum(1 for e in period_events if e.get("lead_id") and e.get("actor_type") == "human" and not e.get("actor"))

    visits = list(db["visitas"].find({}))
    period_visits = [v for v in visits if in_utc_interval(v.get("confirmed_at") or v.get("created_at"), start_utc, end_utc)]
    visit_lead_ids = {v.get("lead_id") for v in period_visits if v.get("lead_id") in lead_by_id}
    won, lost = set(), set()
    for event in period_events:
        to_stage = normalize_result((event.get("meta") or {}).get("to") or event.get("result"))
        if event.get("lead_id") not in lead_by_id:
            continue
        if to_stage == "CLOSED_WON": won.add(event["lead_id"])
        if to_stage == "CLOSED_LOST": lost.add(event["lead_id"])

    temperatures = {lead_id: historical_temperature_at(lead_by_id[lead_id], end_utc) for lead_id in cohort_ids}
    # Preserve partial historical evidence. Unknown is a first-class value and
    # must never be silently converted to Cold or make known counts disappear.
    hot_cutoff = sum(value == "HOT" for value in temperatures.values())
    cold_cutoff = sum(value == "COLD" for value in temperatures.values())
    unknown_cutoff = sum(value not in {"HOT", "COLD"} for value in temperatures.values())
    temperature_invariant_valid = hot_cutoff + cold_cutoff + unknown_cutoff == len(cohort_ids)
    temperature_publishable = unknown_cutoff == 0
    hot_pending_cutoff = sum(lead_id in unmanaged_ids and value == "HOT" for lead_id, value in temperatures.items())
    cold_pending_cutoff = sum(lead_id in unmanaged_ids and value == "COLD" for lead_id, value in temperatures.items())
    unknown_pending_cutoff = sum(
        lead_id in unmanaged_ids and value not in {"HOT", "COLD"}
        for lead_id, value in temperatures.items()
    )

    cycles = list(db["crm_assignment_cycles"].find({}))
    period_cycles = [c for c in cycles if in_utc_interval(c.get("assigned_at"), start_utc, end_utc)]
    active_cycles = [c for c in cycles if not c.get("unassigned_at") and c.get("lead_id") in lead_by_id]
    active_by_lead = {c["lead_id"]: c for c in sorted(active_cycles, key=lambda c: coerce_utc_datetime(c.get("assigned_at")) or datetime.min.replace(tzinfo=timezone.utc))}
    pending_stages = {"NEW", "NEW_LEAD", "NUEVO", ""}
    pending_ids = {
        lead_id for lead_id, lead in lead_by_id.items()
        if str(lead.get("pipeline_stage") or lead.get("stage") or "NEW").upper() in pending_stages
    }
    current_pending_cycles = {lead_id: cycle for lead_id, cycle in active_by_lead.items() if lead_id in pending_ids}
    excluded_unassigned = len(pending_ids - set(current_pending_cycles))
    hot_unattended = sum(
        str(lead_by_id[lead_id].get("lead_temperature_effective") or "").upper() == "HOT"
        for lead_id in current_pending_cycles
    )
    overdue_ids, pending_ages = set(), {}
    for lead_id, cycle in current_pending_cycles.items():
        assigned = coerce_utc_datetime(cycle.get("assigned_at"))
        if not assigned or assigned < cutover_utc:
            continue
        first = coerce_utc_datetime(cycle.get("first_valid_management_at"))
        sla = calculate_sla(assigned_at=assigned, first_valid_management_at=first, now=priority_utc)
        if not first and sla["status"] == "critical": overdue_ids.add(lead_id)
        if not first and sla["minutes"] is not None: pending_ages[lead_id] = sla["minutes"]

    names = list(dict.fromkeys(str(name).strip() for name in executive_order if str(name).strip()))
    buckets = {name: {"name": name, "new_assigned_unique": 0, "managed_unique": 0, "current_pending_unique": 0,
                      "effective_contacts_unique": 0, "leads_with_visit_unique": 0,
                      "closed_won_unique": 0, "closed_lost_unique": 0} for name in names}
    def bucket(name):
        return buckets.get(str(name or "").strip())
    new_sets, managed_sets, pending_sets, effective_sets = {}, {}, {}, {}
    for cycle in period_cycles:
        name = cycle.get("assigned_to_display_name") or cycle.get("assigned_to_user_id")
        if bucket(name): new_sets.setdefault(name, set()).add(cycle.get("lead_id"))
    for event in valid_management:
        name = event.get("actor")
        if bucket(name): managed_sets.setdefault(name, set()).add(event.get("lead_id"))
    for lead_id, cycle in current_pending_cycles.items():
        name = cycle.get("assigned_to_display_name") or cycle.get("assigned_to_user_id")
        if bucket(name): pending_sets.setdefault(name, set()).add(lead_id)
    for event in period_events:
        if event_evidence(event)["effective_contact"] and bucket(event.get("actor")):
            effective_sets.setdefault(event["actor"], set()).add(event.get("lead_id"))
    for name, row in buckets.items():
        row["new_assigned_unique"] = len(new_sets.get(name, set()) - {None})
        row["managed_unique"] = len(managed_sets.get(name, set()) - {None})
        row["current_pending_unique"] = len(pending_sets.get(name, set()) - {None})
        row["effective_contacts_unique"] = len(effective_sets.get(name, set()) - {None})

    oldest = max(pending_ages.values(), default=None)
    limitations = []
    if not temperature_publishable: limitations.append("Temperatura hist\u00f3rica al corte no demostrable para toda la cohorte")
    if ambiguous: limitations.append("Existen eventos ambiguos excluidos")
    snapshot = {
        "schema_version": "crm_weekly_snapshot_v1", "metric_version": METRIC_VERSION,
        "report": {"period_start": str(period_start), "period_end": str(period_end), "timezone": "America/Santiago",
                   "generated_at": utc_now(), "historical_comparison_allowed": False},
        "cohort": {"received_unique": len(cohort_ids), "hot_at_cutoff_unique": hot_cutoff,
                   "cold_at_cutoff_unique": cold_cutoff,
                   "unknown_temperature_at_cutoff_unique": unknown_cutoff,
                   "managed_unique": len(managed_ids),
                   "unmanaged_at_cutoff_unique": len(unmanaged_ids),
                   "hot_unmanaged_at_cutoff_unique": hot_pending_cutoff,
                   "cold_unmanaged_at_cutoff_unique": cold_pending_cutoff,
                   "unknown_temperature_unmanaged_at_cutoff_unique": unknown_pending_cutoff,
                   # Compatibility for the not-yet-active report code.
                   "hot_pending_at_cutoff_unique": hot_pending_cutoff},
        "pipeline_activity": {"leads_with_confirmed_attempt_unique": len(attempts),
                              "leads_with_effective_contact_unique": len(effective),
                              "leads_with_visit_unique": len(visit_lead_ids), "visit_events_total": len(period_visits),
                              "closed_won_unique": len(won), "closed_lost_unique": len(lost)},
        "monday_priorities": {"priority_as_of": priority_utc.astimezone(CHILE_TZ).isoformat(),
                              "hot_unattended_unique": hot_unattended,
                              "sla_overdue_publishable_unique": len(overdue_ids),
                              "oldest_pending_business_minutes": oldest,
                              "oldest_pending_display": format_business_age(oldest)},
        "executives": list(buckets.values()),
        "operational_focus": {"key": "", "supporting_metrics": {}},
        "data_quality": {"cutover": INSTRUMENTATION_CUTOVER, "complete_for_period": True,
                         "temperature_publishable": temperature_publishable, "sla_publishable": True,
                         "sla_definition": "team_first_assignment_to_first_valid_management",
                         "excluded_ambiguous_events": ambiguous,
                         "excluded_unresolved_actor_events": unresolved_actor,
                         "excluded_unassigned_leads": excluded_unassigned,
                         "unknown_temperature_count": unknown_cutoff,
                         "temperature_invariant_valid": temperature_invariant_valid,
                         "limitations": limitations},
        "crm_parity": {"validated": False, "differences": []},
        "_audit": {"cohort_ids": list(cohort_ids), "managed_ids": list(managed_ids), "unmanaged_ids": list(unmanaged_ids)},
    }
    return snapshot
