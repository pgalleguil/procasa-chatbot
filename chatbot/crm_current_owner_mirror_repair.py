"""Guarded repair and read-only verification of current-owner mirrors."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import logging
import time
from typing import Any, Mapping

from pymongo.errors import PyMongoError

from .crm_lead_access import resolve_crm_lead_access_context
from .crm_sla_reassignment_transaction import build_current_cycle_owner_mirror_repair
from .mongo_identity import mongo_id_variants

logger = logging.getLogger(__name__)
REPAIR_VERSION = "crm_owner_mirror_repair_20260922"
_DERIVED_FIELDS = (
    "lifecycle.assigned_to_user_id",
    "lifecycle.assigned_to_display_name",
    "assignment_mirror_owner_user_id",
    "ejecutivo_asignado",
    "prospecto.ejecutivo",
    "assignment_mirror_source",
)
_ACTIVE_CYCLE_PROJECTION = {
    "assignment_cycle_id": 1,
    "lead_id": 1,
    "assigned_to_user_id": 1,
    "assigned_to_display_name": 1,
    "assigned_at": 1,
    "sla_started_at": 1,
    "cycle_status": 1,
    "unassigned_at": 1,
    "reassignment_state": 1,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _existing_path(document: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _find_one_by_variants(collection: Any, field: str, value: Any) -> Mapping[str, Any] | None:
    variants = list(mongo_id_variants(value))
    if not variants:
        return None
    try:
        found = collection.find_one({field: {"$in": variants}})
        if found:
            return found
    except Exception:
        pass
    for candidate in variants:
        found = collection.find_one({field: candidate})
        if found:
            return found
    return None


def _active_cycles_for_lead(collection: Any, lead_id: Any) -> list[Mapping[str, Any]]:
    query = {
        "lead_id": {"$in": list(mongo_id_variants(lead_id))},
        "cycle_status": "active",
        "unassigned_at": None,
    }
    try:
        return list(collection.find(query))
    except Exception:
        result: list[Mapping[str, Any]] = []
        for candidate in mongo_id_variants(lead_id):
            result.extend(list(collection.find({
                "lead_id": candidate,
                "cycle_status": "active",
                "unassigned_at": None,
            })))
        return result


def _group_active_cycles(db: Any) -> tuple[list[Mapping[str, Any]], dict[str, list[Mapping[str, Any]]]]:
    query = {"cycle_status": "active", "unassigned_at": None}
    cycles: list[Mapping[str, Any]] = []
    for attempt in range(3):
        try:
            cursor = db["crm_assignment_cycles"].find(query, _ACTIVE_CYCLE_PROJECTION)
            try:
                cursor = cursor.batch_size(100)
            except AttributeError:
                pass
            cycles = list(cursor)
            break
        except PyMongoError:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for cycle in cycles:
        key = _text(cycle.get("lead_id"))
        if key:
            grouped[key].append(cycle)
    return cycles, grouped


def _load_leads_for_groups(db: Any, grouped: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Mapping[str, Any]]:
    """Load the small lead projection used by repair/verification in one read."""
    variants: list[Any] = []
    for active_cycles in grouped.values():
        for cycle in active_cycles:
            for variant in mongo_id_variants(cycle.get("lead_id")):
                if variant not in variants:
                    variants.append(variant)
    if not variants:
        return {}
    projection = {
        "_id": 1,
        "lifecycle": 1,
        "reassignment_decision_id": 1,
        "automatic_reassignment_number": 1,
        "assigned_by": 1,
        "reassigned_by_sla": 1,
        "reassignment_source": 1,
        "reassigned_from_owner_user_id": 1,
        "reassignment_source_cycle_id": 1,
        "owner_user_id": 1,
        "assigned_to_user_id": 1,
        "ejecutivo_asignado": 1,
        "prospecto.ejecutivo": 1,
    }
    for attempt in range(3):
        try:
            cursor = db["leads"].find({"_id": {"$in": variants}}, projection)
            try:
                cursor = cursor.batch_size(100)
            except AttributeError:
                pass
            return {_text(lead.get("_id")): lead for lead in cursor}
        except PyMongoError:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    return {}


def _lead_for_cycle(lead_map: Mapping[str, Mapping[str, Any]], db: Any, cycle: Mapping[str, Any]) -> Mapping[str, Any] | None:
    lead_id = cycle.get("lead_id")
    return lead_map.get(_text(lead_id)) or _find_one_by_variants(db["leads"], "_id", lead_id)


def _canonical_owner_is_unambiguous(lead: Mapping[str, Any], cycle: Mapping[str, Any]) -> bool:
    owner_id = _text(cycle.get("assigned_to_user_id"))
    owner_name = _text(cycle.get("assigned_to_display_name"))
    cycle_id = _text(cycle.get("assignment_cycle_id"))
    lifecycle = lead.get("lifecycle") if isinstance(lead.get("lifecycle"), Mapping) else {}
    if not owner_id or not owner_name or not cycle_id:
        return False
    if _text(lifecycle.get("current_assignment_cycle_id")) != cycle_id:
        return False
    explicit_ids = {
        _text(lead.get(key))
        for key in ("owner_user_id", "assigned_to_user_id")
        if lead.get(key) not in (None, "")
    }
    return not explicit_ids or explicit_ids == {owner_id}


def _build_derived_update(*, lead: Mapping[str, Any], cycle: Mapping[str, Any], repaired_at: datetime) -> dict[str, Any] | None:
    built = build_current_cycle_owner_mirror_repair(
        lead=lead,
        active_cycles=[cycle],
        repaired_at=repaired_at,
    )
    if not built:
        return None
    source = built.get("$set") or {}
    return {"$set": {field: source[field] for field in _DERIVED_FIELDS if field in source}}


def _true_mirror_conflicts(lead: Mapping[str, Any], cycle: Mapping[str, Any]) -> list[str]:
    owner_id = _text(cycle.get("assigned_to_user_id"))
    owner_name = _text(cycle.get("assigned_to_display_name"))
    expected = {
        "lifecycle.assigned_to_user_id": owner_id,
        "lifecycle.assigned_to_display_name": owner_name,
        "assignment_mirror_owner_user_id": owner_id,
        "ejecutivo_asignado": owner_name,
        "prospecto.ejecutivo": owner_name,
        "assignment_mirror_source": "crm_assignment_cycles",
    }
    conflicts: list[str] = []
    for field, expected_value in expected.items():
        exists, actual = _existing_path(lead, field)
        if exists and actual not in (None, "") and str(actual) != str(expected_value):
            conflicts.append(field)
    return conflicts


def _empty_result() -> dict[str, Any]:
    return {
        "status": "completed",
        "repair_version": REPAIR_VERSION,
        "active_cycles_scanned": 0,
        "true_conflicts_before": 0,
        "repaired": 0,
        "writes": 0,
        "skipped_ambiguous": 0,
        "cas_lost": 0,
        "errors": 0,
        "conflict_lead_ids": [],
    }


def inspect_current_owner_mirror_conflicts(db: Any) -> dict[str, Any]:
    result = _empty_result()
    cycles, grouped = _group_active_cycles(db)
    result["active_cycles_scanned"] = len(cycles)
    lead_map = _load_leads_for_groups(db, grouped)
    conflict_lead_ids: list[str] = []
    for lead_key, active_cycles in grouped.items():
        if len(active_cycles) != 1:
            result["skipped_ambiguous"] += 1
            continue
        cycle = active_cycles[0]
        lead = _lead_for_cycle(lead_map, db, cycle)
        if not lead or not _canonical_owner_is_unambiguous(lead, cycle):
            result["skipped_ambiguous"] += 1
            continue
        if _true_mirror_conflicts(lead, cycle):
            result["true_conflicts_before"] += 1
            conflict_lead_ids.append(str(lead.get("_id", lead_key)))
    result["conflict_lead_ids"] = conflict_lead_ids
    return result


def run_current_owner_mirror_repair_once(db: Any, *, now: datetime | None = None) -> dict[str, Any]:
    result = _empty_result()
    repaired_at = now or datetime.now(timezone.utc)
    cycles, grouped = _group_active_cycles(db)
    result["active_cycles_scanned"] = len(cycles)
    lead_map = _load_leads_for_groups(db, grouped)
    for active_cycles in grouped.values():
        if len(active_cycles) != 1:
            result["skipped_ambiguous"] += 1
            continue
        cycle = active_cycles[0]
        lead = _lead_for_cycle(lead_map, db, cycle)
        if not lead or not _canonical_owner_is_unambiguous(lead, cycle):
            result["skipped_ambiguous"] += 1
            continue
        if not _true_mirror_conflicts(lead, cycle):
            continue
        result["true_conflicts_before"] += 1
        update = _build_derived_update(lead=lead, cycle=cycle, repaired_at=repaired_at)
        if not update:
            result["skipped_ambiguous"] += 1
            continue
        try:
            current_cycles = _active_cycles_for_lead(db["crm_assignment_cycles"], cycle.get("lead_id"))
            if len(current_cycles) != 1 or _text(current_cycles[0].get("assignment_cycle_id")) != _text(cycle.get("assignment_cycle_id")):
                result["cas_lost"] += 1
                continue
            current_lead = _find_one_by_variants(db["leads"], "_id", lead.get("_id"))
            if not current_lead or not _canonical_owner_is_unambiguous(current_lead, cycle):
                result["cas_lost"] += 1
                continue
            if not _true_mirror_conflicts(current_lead, cycle):
                continue
            outcome = db["leads"].update_one({
                "_id": lead.get("_id"),
                "lifecycle.current_assignment_cycle_id": _text(cycle.get("assignment_cycle_id")),
            }, update)
            if getattr(outcome, "matched_count", 0) != 1:
                result["cas_lost"] += 1
                continue
            result["repaired"] += 1
            result["writes"] += 1
        except Exception:
            result["errors"] += 1
            logger.exception("[CRM_OWNER_MIRROR_REPAIR] individual repair failed")
    return result


def validate_current_owner_access_integrity(db: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "completed",
        "evaluated_current_owners": 0,
        "access_allowed": 0,
        "access_denied": 0,
        "denied_reasons": {},
        "exceptions": 0,
    }
    _, grouped = _group_active_cycles(db)
    lead_map = _load_leads_for_groups(db, grouped)
    for active_cycles in grouped.values():
        if len(active_cycles) != 1:
            continue
        cycle = active_cycles[0]
        lead = _lead_for_cycle(lead_map, db, cycle)
        if not lead or not _canonical_owner_is_unambiguous(lead, cycle):
            continue
        user = _find_one_by_variants(db["usuarios"], "_id", cycle.get("assigned_to_user_id"))
        if not user:
            result["exceptions"] += 1
            result["denied_reasons"]["owner_user_missing"] = result["denied_reasons"].get("owner_user_missing", 0) + 1
            continue
        try:
            context = resolve_crm_lead_access_context(db, user=user, lead=lead, security_enabled=True)
            result["evaluated_current_owners"] += 1
            if context.access_allowed and context.http_status == 200 and context.lock_reason is None and context.is_current_owner:
                result["access_allowed"] += 1
            else:
                result["access_denied"] += 1
                reason = context.lock_reason or f"HTTP_{context.http_status}"
                result["denied_reasons"][reason] = result["denied_reasons"].get(reason, 0) + 1
        except Exception:
            result["exceptions"] += 1
            result["denied_reasons"]["resolver_exception"] = result["denied_reasons"].get("resolver_exception", 0) + 1
            logger.exception("[CRM_OWNER_ACCESS_VERIFY] resolver failed")
    return result


__all__ = [
    "REPAIR_VERSION",
    "inspect_current_owner_mirror_conflicts",
    "run_current_owner_mirror_repair_once",
    "validate_current_owner_access_integrity",
]
