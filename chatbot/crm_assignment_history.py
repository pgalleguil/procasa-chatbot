"""Read-only historical assignment rows for the CRM list.

The active CRM list is sourced from the lead's current assignment cycle.  This
module deliberately uses closed assignment cycles for the historical section so
that a previous owner can see an audit-safe record without regaining any
operational access to the lead.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from typing import Any, Mapping

from .crm_metrics import coerce_utc_datetime
from .mongo_identity import mongo_id_variants


HISTORICAL_CYCLE_STATUSES = frozenset({"reassigned", "closed"})
HISTORICAL_FILTER_STATE = "SLA_REASSIGNED_HISTORY"
HISTORICAL_REASONS = frozenset({
    "sla_reassignment",
    "policy_repair_tier1",
    "policy_repair_admin_excluded",
    "orphan_lead_not_found_repair",
})
HISTORICAL_STATE_LABEL = "Reasignado por SLA"
HISTORICAL_SLA_STATE_LABEL = "Vencido"
HISTORICAL_RESPONSE_LABEL = "🔒 Reasignado"
HISTORICAL_REASON_FIELDS = (
    "closed_reason",
    "unassigned_reason",
    "reassignment_reason",
    "reassignment_source",
    "reason",
)
logger = logging.getLogger(__name__)


def _text(value: Any, default: str = "") -> str:
    value = str(value or "").strip()
    return value or default


def _nested(doc: Mapping[str, Any], *paths: str, default: Any = None) -> Any:
    for path in paths:
        value: Any = doc
        found = True
        for part in path.split("."):
            if not isinstance(value, Mapping) or part not in value:
                found = False
                break
            value = value[part]
        if found and value not in (None, ""):
            return value
    return default


def _same_id(left: Any, right: Any) -> bool:
    return left not in (None, "") and right not in (None, "") and str(left) == str(right)


def _is_sla_historical_cycle(cycle: Mapping[str, Any]) -> bool:
    status = _text(cycle.get("cycle_status")).casefold()
    reasons = {
        _text(cycle.get(field)).casefold()
        for field in HISTORICAL_REASON_FIELDS
        if _text(cycle.get(field))
    }
    has_reason = bool(reasons & HISTORICAL_REASONS)
    has_reassignment_evidence = any(
        cycle.get(field) not in (None, "")
        for field in (
            "reassigned_at",
            "reassignment_decision_id",
            "reassignment_source_cycle_id",
            "reassignment_new_cycle_id",
            "reassigned_to_user_id",
        )
    )
    has_breach_evidence = any(
        cycle.get(field) not in (None, "")
        for field in (
            "sla_breached_at",
            "breached_at",
            "source_cycle_sla_breached_at",
            "previous_cycle_sla_breached_at",
        )
    )
    return (
        status in HISTORICAL_CYCLE_STATUSES
        and status != "active"
        and (has_reason or bool(cycle.get("reassigned_by_sla")))
        and (has_reassignment_evidence or has_breach_evidence)
    )


def _historical_user_ids_by_name(db: Any, user_name: Any) -> list[Any]:
    """Resolve the admin's display filter to canonical user ids only."""
    name = _text(user_name)
    if not name or name.casefold() == "todos":
        return []
    exact_name = re.compile(rf"^{re.escape(name)}$", re.IGNORECASE)
    projection = {"_id": 1}
    matches = db["usuarios"].find(
        {"$or": [{"nombre": exact_name}, {"display_name": exact_name}, {"name": exact_name}]},
        projection,
    )
    variants: list[Any] = []
    for match in matches:
        for value in mongo_id_variants(match.get("_id")):
            if value not in variants:
                variants.append(value)
    return variants


def _property_code(lead: Mapping[str, Any] | None, cycle: Mapping[str, Any]) -> str:
    lead = lead or {}
    return _text(
        cycle.get("property_code")
        or _nested(lead, "property_code", "codigo", "prospecto.codigo", "datos_propiedad.codigo"),
        "S/N",
    )


def _operation(lead: Mapping[str, Any] | None, cycle: Mapping[str, Any]) -> str:
    lead = lead or {}
    return _text(
        cycle.get("operation")
        or cycle.get("operacion")
        or _nested(lead, "operation", "operacion", "prospecto.operacion", "datos_propiedad.operacion"),
        "S/I",
    )


def _commune(lead: Mapping[str, Any] | None, cycle: Mapping[str, Any]) -> str:
    lead = lead or {}
    return _text(
        cycle.get("commune")
        or cycle.get("comuna")
        or _nested(lead, "commune", "comuna", "prospecto.comuna", "datos_propiedad.comuna"),
        "S/I",
    )


def _display_datetime(value: Any) -> datetime | None:
    parsed = coerce_utc_datetime(value)
    return parsed.astimezone(timezone.utc) if parsed else None


def build_historical_assignment_row(
    cycle: Mapping[str, Any],
    lead: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the intentionally small, non-operational history payload.

    Do not add phone/email or a detail URL here.  The template can therefore
    render the row without relying on a client-side redaction convention.
    """
    lead = lead or {}
    temperature = _text(
        cycle.get("temperature_at_assignment")
        or lead.get("lead_temperature_effective"),
        "NORMAL",
    ).upper()
    original_assigned_at = _display_datetime(cycle.get("assigned_at"))
    breached_at = _display_datetime(
        cycle.get("sla_breached_at")
        or cycle.get("breached_at")
        or cycle.get("source_cycle_sla_breached_at")
        or cycle.get("previous_cycle_sla_breached_at")
    )
    reassigned_at = _display_datetime(
        cycle.get("reassigned_at")
        or cycle.get("closed_at")
        or cycle.get("unassigned_at")
    )
    return {
        "historical": True,
        "actions_disabled": True,
        "operational_url": None,
        "lead_id": str(lead.get("_id") or cycle.get("lead_id") or ""),
        "assignment_cycle_id": _text(cycle.get("assignment_cycle_id")),
        "cliente": _text(
            cycle.get("client_name")
            or cycle.get("lead_name")
            or _nested(lead, "prospecto.nombre"),
            "Desconocido",
        ),
        "codigo_propiedad": _property_code(lead, cycle),
        "operacion": _operation(lead, cycle),
        "comuna": _commune(lead, cycle),
        "assigned_at": original_assigned_at,
        "assigned_at_display": original_assigned_at,
        "lead_temperature_effective": temperature if temperature in {"HOT", "NORMAL", "COLD"} else "NORMAL",
        "sla_label": "HOT" if temperature == "HOT" else "NORMAL",
        "breached_at": breached_at,
        "breached_at_display": breached_at,
        "reassigned_at": reassigned_at,
        "reassigned_at_display": reassigned_at,
        "sla_state_label": HISTORICAL_SLA_STATE_LABEL,
        "status_label": HISTORICAL_STATE_LABEL,
        "response_label": HISTORICAL_RESPONSE_LABEL,
        "management_before_breach": _management_before_breach(cycle, breached_at),
        "previous_owner_user_id": _text(cycle.get("assigned_to_user_id")),
        "previous_owner_name": _text(cycle.get("assigned_to_display_name"), "Ejecutivo"),
    }


def _management_before_breach(
    cycle: Mapping[str, Any], breached_at: datetime | None
) -> str:
    if breached_at is None:
        return "S/I"
    management_at = _display_datetime(
        cycle.get("first_valid_management_at")
        or cycle.get("first_effective_contact_at")
        or cycle.get("first_management_at")
    )
    if management_at is None:
        return "No"
    return "Sí" if management_at <= breached_at else "No"


def get_historical_assignment_rows(
    db: Any,
    *,
    user_id: Any = None,
    user_name: Any = None,
    limit: int = 250,
) -> list[dict[str, Any]]:
    """Return historical SLA rows sourced from non-active assignment cycles."""
    query: dict[str, Any] = {
        "cycle_status": {"$ne": "active"},
        "$or": [
            {"closed_reason": {"$in": list(HISTORICAL_REASONS)}},
            {"unassigned_reason": {"$in": list(HISTORICAL_REASONS)}},
            {"reassignment_reason": {"$in": list(HISTORICAL_REASONS)}},
            {"reassignment_source": {"$in": list(HISTORICAL_REASONS)}},
            {"reason": {"$in": list(HISTORICAL_REASONS)}},
            {"reassigned_by_sla": True},
            {"reassignment_decision_id": {"$exists": True}},
            {"reassignment_source_cycle_id": {"$exists": True}},
        ],
    }
    if user_id not in (None, ""):
        query["assigned_to_user_id"] = {"$in": list(mongo_id_variants(user_id))}
    elif user_name not in (None, "") and _text(user_name).casefold() != "todos":
        user_ids = _historical_user_ids_by_name(db, user_name)
        if not user_ids:
            return []
        query["assigned_to_user_id"] = {"$in": user_ids}

    try:
        cycles = list(
            db["crm_assignment_cycles"]
            .find(query)
            .sort([("reassigned_at", -1), ("closed_at", -1), ("assigned_at", -1)])
            .limit(max(1, int(limit)))
        )
    except Exception:
        # History is additive UI context.  A temporary read failure must not
        # turn the operational CRM list into a 500.
        logger.exception("CRM historical assignment read failed")
        return []
    if not cycles:
        return []

    lead_values = [cycle.get("lead_id") for cycle in cycles if cycle.get("lead_id") not in (None, "")]
    lead_lookup_values: list[Any] = []
    for value in lead_values:
        for variant in mongo_id_variants(value):
            if variant not in lead_lookup_values:
                lead_lookup_values.append(variant)
    leads_by_id: dict[str, Mapping[str, Any]] = {}
    try:
        for lead in db["leads"].find(
            {"_id": {"$in": lead_lookup_values}},
            {
                "_id": 1,
                "prospecto.nombre": 1,
                "prospecto.codigo": 1,
                "prospecto.operacion": 1,
                "prospecto.comuna": 1,
                "datos_propiedad.codigo": 1,
                "datos_propiedad.operacion": 1,
                "datos_propiedad.comuna": 1,
                "property_code": 1,
                "operation": 1,
                "operacion": 1,
                "commune": 1,
                "comuna": 1,
                "lead_temperature_effective": 1,
            },
        ):
            leads_by_id[str(lead.get("_id"))] = lead
    except Exception:
        logger.exception("CRM historical lead snapshot read failed")
        leads_by_id = {}

    current_cycle_ids: set[str] = set()
    try:
        active_cursor = db["crm_assignment_cycles"].find({
            "cycle_status": "active",
            "unassigned_at": None,
            "lead_id": {"$in": lead_lookup_values},
        }, {"assignment_cycle_id": 1})
        for active in active_cursor:
            if active.get("assignment_cycle_id") not in (None, ""):
                current_cycle_ids.add(str(active["assignment_cycle_id"]))
    except Exception:
        logger.exception("CRM current cycle read failed for historical list")
        return []

    rows: list[dict[str, Any]] = []
    destination_by_source: dict[str, Mapping[str, Any]] = {}
    try:
        destination_cursor = db["crm_assignment_cycles"].find({
            "$or": [
                {"reassignment_source_cycle_id": {"$in": [str(cycle.get("assignment_cycle_id")) for cycle in cycles]}},
                {"previous_assignment_cycle_id": {"$in": [str(cycle.get("assignment_cycle_id")) for cycle in cycles]}},
            ],
            "lead_id": {"$in": lead_lookup_values},
        })
        for destination in destination_cursor:
            for key in ("reassignment_source_cycle_id", "previous_assignment_cycle_id"):
                source_id = _text(destination.get(key))
                if source_id:
                    destination_by_source[source_id] = destination
    except Exception:
        logger.exception("CRM historical destination-cycle read failed")

    seen: set[tuple[str, str]] = set()
    for cycle in cycles:
        cycle_id = _text(cycle.get("assignment_cycle_id"))
        lead_key = _text(cycle.get("lead_id"))
        identity = (lead_key, cycle_id)
        if not cycle_id or identity in seen or cycle_id in current_cycle_ids:
            continue
        if not _is_sla_historical_cycle(cycle):
            continue
        effective_cycle = dict(cycle)
        destination = destination_by_source.get(cycle_id) or {}
        for field in (
            "sla_breached_at",
            "breached_at",
            "source_cycle_sla_breached_at",
            "previous_cycle_sla_breached_at",
        ):
            if effective_cycle.get(field) in (None, "") and destination.get(field) not in (None, ""):
                effective_cycle[field] = destination[field]
        row = build_historical_assignment_row(effective_cycle, leads_by_id.get(str(cycle.get("lead_id"))))
        if not row["lead_id"]:
            continue
        seen.add(identity)
        rows.append(row)
    return rows


__all__ = [
    "HISTORICAL_STATE_LABEL",
    "HISTORICAL_FILTER_STATE",
    "HISTORICAL_RESPONSE_LABEL",
    "build_historical_assignment_row",
    "get_historical_assignment_rows",
]
