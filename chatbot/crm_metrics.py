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

from .constants import CHILE_TZ
from .utils import calculate_business_minutes

METRIC_VERSION = "crm_metrics_v1"
INSTRUMENTATION_CUTOVER = "2026-07-20T00:00:00-04:00"

OPEN_ONLY_EVENT_TYPES = frozenset({
    "CLICK_WHATSAPP_LEAD", "CLICK_PHONE_LEAD", "CLICK_EMAIL_LEAD",
    "CLICK_WHATSAPP_OWNER", "CLICK_PHONE_OWNER", "CLICK_EMAIL_OWNER",
    "OPEN_DETAIL", "NAVIGATION", "FILTER", "ASSIGNMENT", "assignment",
    "ALERT", "ALERT_SENT", "alert_sent", "BOT_MSG",
})
VALID_MANAGEMENT_EVENT_TYPES = frozenset({
    "GESTION_LOG", "HUMAN_NOTE", "CONTACT_RESULT", "STATUS_CHANGE",
    "SEND_WA_LEAD", "SEND_EMAIL_LEAD", "MANUAL_ENTRY",
})
CONTACT_ATTEMPT_RESULTS = frozenset({
    "NO_RESPONDIO", "OCUPADO", "NUMERO_INVALIDO", "MENSAJE_ENVIADO",
    "CONTACTADO", "SOLICITA_SEGUIMIENTO", "NO_INTERESADO", "OTRO",
})
EFFECTIVE_CONTACT_RESULTS = frozenset({
    "CONTACTADO", "SOLICITA_SEGUIMIENTO", "NO_INTERESADO",
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
        attempt or (
            event_type in VALID_MANAGEMENT_EVENT_TYPES
            and event_type != "CONTACT_RESULT"
            and (result is not None or meaningful_change)
        )
    )
    return {
        "human": human, "management": management, "contact_attempt": attempt,
        "effective_contact": effective, "result": result,
    }


def unique_managed_lead_ids(events: Iterable[Mapping[str, Any]]) -> set[Any]:
    return {event["lead_id"] for event in events if event_evidence(event)["management"]}


def create_assignment_cycle(db, *, lead, assigned_to_user_id, assigned_by,
                            reason, assigned_at=None) -> dict[str, Any]:
    assigned_at = coerce_utc_datetime(assigned_at) or utc_now()
    active = db["crm_assignment_cycles"].find_one({"lead_id": lead["_id"], "unassigned_at": None})
    if active and str(active.get("assigned_to_user_id")) == str(assigned_to_user_id):
        return active
    if active:
        db["crm_assignment_cycles"].update_one(
            {"_id": active["_id"], "unassigned_at": None}, {"$set": {"unassigned_at": assigned_at}}
        )
    cycle = {
        "assignment_cycle_id": str(uuid.uuid4()), "lead_id": lead["_id"],
        "assigned_to_user_id": assigned_to_user_id, "assigned_at": assigned_at,
        "unassigned_at": None, "assigned_by": assigned_by, "reason": reason,
        "metric_version": METRIC_VERSION,
    }
    db["crm_assignment_cycles"].insert_one(cycle)
    return cycle


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
    return {"status": status, "minutes": minutes, "fulfilled": bool(end)}


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
