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
HISTORICAL_RESPONSE_LABEL = "🔒 Reasignado por SLA"
HISTORICAL_REASON_FIELDS = (
    "closed_reason",
    "unassigned_reason",
    "reassignment_reason",
    "reassignment_source",
    "reason",
)
logger = logging.getLogger(__name__)

POLICY_REPAIR_FIELDS = (
    "policy_repair_id",
    "policy_repair_reason",
    "repair_type",
    "repair_reason",
    "admin_repair_id",
    "admin_repair_reason",
)
LEDGER_COLLECTION = "crm_sla_reassignment_audit_v1"
COMMITTED_EVENT = "SLA_REASSIGNMENT_COMMITTED"
HISTORICAL_REASON_PATTERN = re.compile(
    r"(sla[_ -]?reassignment|policy[_ -]?repair|admin[_ -]?repair)",
    re.IGNORECASE,
)


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


def _reason_values(doc: Mapping[str, Any]) -> set[str]:
    values = set()
    for field in (*HISTORICAL_REASON_FIELDS, "assigned_by", "source"):
        raw_value = doc.get(field)
        raw_values = raw_value if isinstance(raw_value, (list, tuple, set)) else (raw_value,)
        for item in raw_values:
            value = _text(item).casefold()
            if value:
                values.add(value)
    return values


def _is_policy_repair(doc: Mapping[str, Any]) -> bool:
    for field in POLICY_REPAIR_FIELDS:
        value = doc.get(field)
        if value not in (None, "", False, [], {}):
            return True
    for value in _reason_values(doc):
        if re.search(r"(policy[_ -]?repair|admin[_ -]?repair)", value, re.IGNORECASE):
            return True
    return False


def _has_breach_or_reassignment_evidence(doc: Mapping[str, Any]) -> bool:
    return any(
        doc.get(field) not in (None, "")
        for field in (
            "sla_breached_at",
            "breached_at",
            "source_cycle_sla_breached_at",
            "previous_cycle_sla_breached_at",
            "reassigned_at",
            "reassignment_decision_id",
            "reassignment_source_cycle_id",
            "reassignment_new_cycle_id",
            "reassigned_to_user_id",
        )
    )


def _is_real_sla_cycle(
    cycle: Mapping[str, Any], ledger_source_ids: set[str] | None = None
) -> bool:
    """Identify an actual SLA episode root, excluding technical repairs."""
    status = _text(cycle.get("cycle_status")).casefold()
    if status not in HISTORICAL_CYCLE_STATUSES or _is_policy_repair(cycle):
        return False
    reasons = _reason_values(cycle)
    source_id = _text(cycle.get("assignment_cycle_id"))
    has_sla_reason = any(HISTORICAL_REASON_PATTERN.search(value) for value in reasons)
    has_policy_reason = any(
        re.search(r"(policy[_ -]?repair|admin[_ -]?repair)", value, re.IGNORECASE)
        for value in reasons
    )
    if has_policy_reason:
        return False
    has_ledger_source = bool(source_id and source_id in (ledger_source_ids or set()))
    return (
        (has_sla_reason or bool(cycle.get("reassigned_by_sla")) or has_ledger_source)
        and (has_ledger_source or _has_breach_or_reassignment_evidence(cycle))
    )


def _is_sla_historical_cycle(cycle: Mapping[str, Any]) -> bool:
    return _is_real_sla_cycle(cycle)


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


def _cycle_key(cycle: Mapping[str, Any]) -> tuple[str, str]:
    return (_text(cycle.get("lead_id")), _text(cycle.get("assignment_cycle_id")))


def _cycle_pointer(cycle: Mapping[str, Any]) -> str:
    return _text(
        cycle.get("reassignment_source_cycle_id")
        or cycle.get("previous_assignment_cycle_id")
        or cycle.get("source_cycle_id")
    )


def _ledger_field_values(event: Mapping[str, Any], *fields: str) -> list[str]:
    values: list[str] = []
    for field in fields:
        value = _text(event.get(field))
        if value and value not in values:
            values.append(value)
    return values


def _ledger_is_repair(event: Mapping[str, Any]) -> bool:
    return _is_policy_repair(event)


def _cycle_sort_key(cycle: Mapping[str, Any]) -> tuple[datetime, str]:
    timestamp = _display_datetime(
        cycle.get("assigned_at")
        or cycle.get("reassigned_at")
        or cycle.get("closed_at")
        or cycle.get("unassigned_at")
    ) or datetime.min.replace(tzinfo=timezone.utc)
    return timestamp, _text(cycle.get("assignment_cycle_id"))


def _find_root_cycle(
    cycle: Mapping[str, Any],
    cycles_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    ledger_by_destination: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    current = cycle
    visited: set[str] = set()
    for _ in range(20):
        current_id = _text(current.get("assignment_cycle_id"))
        if not current_id or current_id in visited:
            break
        visited.add(current_id)
        source_id = _cycle_pointer(current)
        if not source_id:
            event = ledger_by_destination.get(current_id) or {}
            source_id = _text(event.get("source_cycle_id"))
        if not source_id:
            break
        source = cycles_by_key.get((_text(current.get("lead_id")), source_id))
        if not source:
            break
        current = source
    return current


def _destination_cycles(
    cycle: Mapping[str, Any],
    cycles_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    cycles_by_source: Mapping[str, list[Mapping[str, Any]]],
    ledger_by_source: Mapping[str, list[Mapping[str, Any]]],
) -> list[Mapping[str, Any]]:
    lead_id = _text(cycle.get("lead_id"))
    cycle_id = _text(cycle.get("assignment_cycle_id"))
    candidate_ids: list[str] = []
    direct_id = _text(cycle.get("reassignment_new_cycle_id"))
    if direct_id:
        candidate_ids.append(direct_id)
    for event in ledger_by_source.get(cycle_id, []):
        for field in ("destination_cycle_id", "new_cycle_id", "reassignment_new_cycle_id"):
            value = _text(event.get(field))
            if value and value not in candidate_ids:
                candidate_ids.append(value)

    destinations: list[Mapping[str, Any]] = []
    for destination_id in candidate_ids:
        destination = cycles_by_key.get((lead_id, destination_id))
        if destination and destination not in destinations:
            destinations.append(destination)
    for destination in cycles_by_source.get(cycle_id, []):
        if _text(destination.get("lead_id")) == lead_id and destination not in destinations:
            destinations.append(destination)
    return sorted(destinations, key=_cycle_sort_key)


def _chain_from_root(
    root: Mapping[str, Any],
    cycles_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    cycles_by_source: Mapping[str, list[Mapping[str, Any]]],
    ledger_by_source: Mapping[str, list[Mapping[str, Any]]],
) -> list[Mapping[str, Any]]:
    chain: list[Mapping[str, Any]] = [root]
    visited = {_text(root.get("assignment_cycle_id"))}
    current = root
    for _ in range(20):
        destinations = _destination_cycles(
            current, cycles_by_key, cycles_by_source, ledger_by_source
        )
        destinations = [
            destination
            for destination in destinations
            if _text(destination.get("assignment_cycle_id")) not in visited
        ]
        if not destinations:
            break
        # A repair can point back to the root while the root's legacy direct
        # pointer already points at the final cycle.  Follow the immediate
        # predecessor in that case instead of skipping the repair node.
        immediate = [
            item for item in destinations
            if _cycle_pointer(item) == _text(current.get("assignment_cycle_id"))
        ]
        if immediate:
            next_cycle = sorted(immediate, key=_cycle_sort_key)[0]
        else:
            direct_id = _text(current.get("reassignment_new_cycle_id"))
            next_cycle = next(
                (item for item in destinations if _text(item.get("assignment_cycle_id")) == direct_id),
                destinations[0],
            )
        chain.append(next_cycle)
        next_id = _text(next_cycle.get("assignment_cycle_id"))
        visited.add(next_id)
        current = next_cycle
    return chain


def _user_names_by_id(db: Any, ids: list[Any]) -> dict[str, str]:
    lookup: list[Any] = []
    for value in ids:
        for variant in mongo_id_variants(value):
            if variant not in lookup:
                lookup.append(variant)
    if not lookup:
        return {}
    names: dict[str, str] = {}
    try:
        for user in db["usuarios"].find(
            {"_id": {"$in": lookup}},
            {"_id": 1, "nombre": 1, "display_name": 1, "name": 1},
        ):
            name = _text(user.get("nombre") or user.get("display_name") or user.get("name"))
            if name:
                for variant in mongo_id_variants(user.get("_id")):
                    names[str(variant)] = name
    except Exception:
        logger.exception("CRM historical user-name read failed")
    return names


def _owner_name(cycle: Mapping[str, Any], names_by_id: Mapping[str, str]) -> str:
    display_name = _text(cycle.get("assigned_to_display_name"))
    if display_name:
        return display_name
    owner_id = cycle.get("assigned_to_user_id")
    return _text(names_by_id.get(str(owner_id)), "Ejecutivo")


def _management_label(value: str) -> str:
    return {"Sí": "Con gestión", "No": "Sin gestión"}.get(value, value)


def _trace_for_chain(
    chain: list[Mapping[str, Any]],
    final_owner_name: str,
    names_by_id: Mapping[str, str],
) -> list[dict[str, str]]:
    if not chain:
        return []
    trace = [{"owner_name": _owner_name(chain[0], names_by_id), "transition": "vencimiento SLA"}]
    for index, cycle in enumerate(chain[1:], start=1):
        is_repair = _is_policy_repair(cycle)
        is_final = index == len(chain) - 1 or _text(cycle.get("cycle_status")).casefold() == "active"
        transition = "corrección de política" if is_repair else "owner final" if is_final else "reasignación SLA"
        trace.append({"owner_name": _owner_name(cycle, names_by_id), "transition": transition})
    if final_owner_name and (not trace or trace[-1]["owner_name"] != final_owner_name):
        trace.append({"owner_name": final_owner_name, "transition": "owner final"})
    return trace


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
    *,
    final_owner_name: str | None = None,
    final_owner_user_id: Any = None,
    chain: list[Mapping[str, Any]] | None = None,
    names_by_id: Mapping[str, str] | None = None,
    include_trace: bool = False,
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
    names_by_id = names_by_id or {}
    previous_owner_name = _owner_name(cycle, names_by_id)
    final_owner_name = _text(final_owner_name or previous_owner_name)
    row = {
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
        "management_label": _management_label(_management_before_breach(cycle, breached_at)),
        "previous_owner_user_id": _text(cycle.get("assigned_to_user_id")),
        "previous_owner_name": previous_owner_name,
        "final_owner_user_id": _text(final_owner_user_id),
        "final_owner_name": final_owner_name,
    }
    if include_trace:
        row["trace"] = _trace_for_chain(chain or [cycle], final_owner_name, names_by_id)
    return row


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
    include_trace: bool = False,
) -> list[dict[str, Any]]:
    """Return one read-only row per root SLA episode.

    A policy repair is a technical continuation of the root episode, not a
    second commercial reassignment.  The chain is reconstructed from explicit
    cycle pointers and the committed ledger, while the UI payload remains
    operationally inert.
    """
    reason_query = {"$regex": HISTORICAL_REASON_PATTERN}
    query: dict[str, Any] = {
        "cycle_status": {"$ne": "active"},
        "$or": [
            {field: reason_query} for field in HISTORICAL_REASON_FIELDS
        ] + [
            {"reassigned_by_sla": True},
            {"reassignment_decision_id": {"$exists": True}},
            {"reassignment_source_cycle_id": {"$exists": True}},
            {"previous_assignment_cycle_id": {"$exists": True}},
            {"reassignment_new_cycle_id": {"$exists": True}},
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
            .limit(max(1000, min(5000, int(limit) * 4)))
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

    # Load the complete cycle chain for the affected leads.  This is necessary
    # because a repair cycle can be owned by a different user than the root
    # SLA cycle selected by the executive filter.
    all_cycles: list[Mapping[str, Any]] = []
    try:
        all_cycles = list(
            db["crm_assignment_cycles"].find(
                {"lead_id": {"$in": lead_lookup_values}},
                {
                    "_id": 0,
                    "lead_id": 1,
                    "assignment_cycle_id": 1,
                    "assigned_to_user_id": 1,
                    "assigned_to_display_name": 1,
                    "assigned_at": 1,
                    "cycle_status": 1,
                    "unassigned_at": 1,
                    "closed_at": 1,
                    "closed_reason": 1,
                    "unassigned_reason": 1,
                    "reassignment_reason": 1,
                    "reassignment_source": 1,
                    "reason": 1,
                    "assigned_by": 1,
                    "source": 1,
                    "reassigned_by_sla": 1,
                    "sla_breached_at": 1,
                    "breached_at": 1,
                    "source_cycle_sla_breached_at": 1,
                    "previous_cycle_sla_breached_at": 1,
                    "reassigned_at": 1,
                    "reassigned_to_user_id": 1,
                    "reassignment_decision_id": 1,
                    "reassignment_new_cycle_id": 1,
                    "reassignment_source_cycle_id": 1,
                    "previous_assignment_cycle_id": 1,
                    "temperature_at_assignment": 1,
                    "first_valid_management_at": 1,
                    "first_effective_contact_at": 1,
                    "first_management_at": 1,
                    **{field: 1 for field in POLICY_REPAIR_FIELDS},
                },
            )
        )
    except Exception:
        logger.exception("CRM current cycle read failed for historical list")
        return []

    cycles_by_key = {
        _cycle_key(cycle): cycle
        for cycle in all_cycles
        if _cycle_key(cycle)[0] and _cycle_key(cycle)[1]
    }
    cycles_by_source: dict[str, list[Mapping[str, Any]]] = {}
    for destination in all_cycles:
        source_id = _cycle_pointer(destination)
        if source_id:
            cycles_by_source.setdefault(source_id, []).append(destination)

    ledger_events: list[Mapping[str, Any]] = []
    try:
        cycle_ids = [key[1] for key in cycles_by_key]
        ledger_events = list(
            db[LEDGER_COLLECTION].find(
                {
                    "$or": [
                        {"lead_id": {"$in": lead_lookup_values}},
                        {"source_cycle_id": {"$in": cycle_ids}},
                        {"destination_cycle_id": {"$in": cycle_ids}},
                    ],
                    "event_type": COMMITTED_EVENT,
                },
                {
                    "_id": 0,
                    "event_type": 1,
                    "decision_id": 1,
                    "lead_id": 1,
                    "source_cycle_id": 1,
                    "destination_cycle_id": 1,
                    "new_cycle_id": 1,
                    "reassignment_new_cycle_id": 1,
                    "target_owner_user_id": 1,
                    "selected_user_id": 1,
                    "target_owner_display_name": 1,
                    "selected_user_display_name": 1,
                    "source_cycle_sla_breached_at": 1,
                    "sla_breached_at": 1,
                    "reassigned_at": 1,
                    "commit_time": 1,
                    "created_at": 1,
                    "assigned_by": 1,
                    "reassignment_reason": 1,
                    **{field: 1 for field in POLICY_REPAIR_FIELDS},
                    "reverted": 1,
                    "reversed": 1,
                    "reversal": 1,
                },
            )
        )
    except Exception:
        logger.exception("CRM historical ledger read failed")
        ledger_events = []

    ledger_by_source: dict[str, list[Mapping[str, Any]]] = {}
    ledger_by_destination: dict[str, Mapping[str, Any]] = {}
    ledger_source_ids: set[str] = set()
    for event in ledger_events:
        if _ledger_is_repair(event):
            continue
        source_ids = _ledger_field_values(event, "source_cycle_id")
        destination_ids = _ledger_field_values(
            event, "destination_cycle_id", "new_cycle_id", "reassignment_new_cycle_id"
        )
        for source_id in source_ids:
            ledger_source_ids.add(source_id)
            ledger_by_source.setdefault(source_id, []).append(event)
        for destination_id in destination_ids:
            ledger_by_destination[destination_id] = event

    candidate_roots: dict[tuple[str, str], Mapping[str, Any]] = {}
    for cycle in cycles:
        cycle_id = _text(cycle.get("assignment_cycle_id"))
        if not cycle_id:
            continue
        root = _find_root_cycle(cycle, cycles_by_key, ledger_by_destination)
        root_key = _cycle_key(root)
        if not root_key[0] or not root_key[1]:
            continue
        if _is_real_sla_cycle(root, ledger_source_ids):
            candidate_roots[root_key] = root

    selected_ids = set(str(value) for value in mongo_id_variants(user_id)) if user_id not in (None, "") else set()
    if user_name not in (None, "") and _text(user_name).casefold() != "todos":
        selected_ids = {str(value) for value in _historical_user_ids_by_name(db, user_name)}

    names_by_id = _user_names_by_id(
        db,
        [cycle.get("assigned_to_user_id") for cycle in all_cycles]
        + [cycle.get("reassigned_to_user_id") for cycle in all_cycles],
    )
    rows: list[dict[str, Any]] = []
    seen_roots: set[tuple[str, str]] = set()
    for root in sorted(candidate_roots.values(), key=_cycle_sort_key, reverse=True):
        root_key = _cycle_key(root)
        if root_key in seen_roots:
            continue
        if selected_ids and not any(
            str(value) in selected_ids for value in mongo_id_variants(root.get("assigned_to_user_id"))
        ):
            continue
        chain = _chain_from_root(root, cycles_by_key, cycles_by_source, ledger_by_source)
        final_cycle = next(
            (
                item for item in reversed(chain)
                if _text(item.get("cycle_status")).casefold() == "active"
                and item.get("unassigned_at") in (None, "")
            ),
            chain[-1],
        )
        final_owner_id = final_cycle.get("assigned_to_user_id") or root.get("reassigned_to_user_id")
        final_owner_name = _owner_name(final_cycle, names_by_id)
        if final_owner_name == "Ejecutivo" and root.get("reassigned_to_user_id"):
            final_owner_name = _text(names_by_id.get(str(root.get("reassigned_to_user_id"))), "Ejecutivo")
        effective_root = dict(root)
        root_event = ledger_by_source.get(root_key[1], [{}])[0]
        for field in (
            "sla_breached_at",
            "breached_at",
            "source_cycle_sla_breached_at",
            "previous_cycle_sla_breached_at",
            "reassigned_at",
        ):
            if effective_root.get(field) in (None, ""):
                if root_event.get(field) not in (None, ""):
                    effective_root[field] = root_event[field]
                    continue
                ledger_field = {
                    "sla_breached_at": ("source_cycle_sla_breached_at", "sla_breached_at"),
                    "breached_at": ("source_cycle_sla_breached_at", "sla_breached_at"),
                    "source_cycle_sla_breached_at": ("source_cycle_sla_breached_at", "sla_breached_at"),
                    "previous_cycle_sla_breached_at": ("source_cycle_sla_breached_at", "sla_breached_at"),
                    "reassigned_at": ("reassigned_at", "commit_time", "created_at"),
                }.get(field, ())
                for ledger_field_name in ledger_field:
                    if root_event.get(ledger_field_name) not in (None, ""):
                        effective_root[field] = root_event[ledger_field_name]
                        break
                if effective_root.get(field) not in (None, ""):
                    continue
                for item in chain[1:]:
                    if item.get(field) not in (None, ""):
                        effective_root[field] = item[field]
                        break
        row = build_historical_assignment_row(
            effective_root,
            leads_by_id.get(str(root.get("lead_id"))),
            final_owner_name=final_owner_name,
            final_owner_user_id=final_owner_id,
            chain=chain,
            names_by_id=names_by_id,
            include_trace=include_trace,
        )
        if not row["lead_id"]:
            continue
        seen_roots.add(root_key)
        rows.append(row)
        if len(rows) >= max(1, int(limit)):
            break
    return rows


__all__ = [
    "HISTORICAL_STATE_LABEL",
    "HISTORICAL_FILTER_STATE",
    "HISTORICAL_RESPONSE_LABEL",
    "build_historical_assignment_row",
    "get_historical_assignment_rows",
]
